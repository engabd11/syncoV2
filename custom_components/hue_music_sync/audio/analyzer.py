"""Real-time audio feature extraction: frequency bands + beat detection.

Stateful and cheap: each ``push`` of one hop (~20 ms) slides a Hann-windowed
FFT, buckets power into the configured frequency bands with per-band automatic
gain control (so loud and quiet tracks both map to 0..1), and runs onset
detection with an adaptive threshold for beat flags.

Onsets use the **SuperFlux** method (Böck & Widmer, DAFx 2013): spectral flux
on log-compressed magnitudes against a maximum-filtered previous spectrum, so
vibrato, pitch slides and vocal level wobble stop registering as "beats". Two
onset streams are produced:

* broadband flux — feeds tempo estimation (every rhythmic event helps there);
* **bass flux** (< ~200 Hz) — the kick/bass-line onsets that should drive
  anything *visible* (flashes, waves, colour steps). Vocals and hi-hats live
  above this band, which is exactly why lights keyed to broadband onsets feel
  random on busy music.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ..const import (
    ANALYSIS_HOP,
    ANALYSIS_NOISE_FLOOR,
    ANALYSIS_SAMPLE_RATE,
    ANALYSIS_WINDOW,
    BANDS,
)


@dataclass(slots=True)
class AnalysisFrame:
    """One frame of audio features, all band values normalised to 0..1."""

    bands: dict[str, float] = field(default_factory=dict)
    energy: float = 0.0  # broadband RMS energy, normalised
    beat: bool = False
    beat_strength: float = 0.0  # how far flux exceeded threshold, 0..~3
    tempo_bpm: float | None = None
    flux: float = 0.0  # onset strength (spectral flux), normalised 0..~1
    t_audio: float = 0.0  # monotonic decode-time of this frame (seconds)
    centroid: float = 0.0  # spectral centroid 0..1 (brightness of the sound)
    bass_flux: float = 0.0  # low-band onset strength, normalised 0..~1
    bass_beat: bool = False  # a kick/bass onset (drives visible accents)
    bass_strength: float = 0.0  # how far bass flux exceeded threshold, 0..~3
    mid_flux: float = 0.0  # mid-band (guitar/snare) onset strength
    mid_beat: bool = False  # a guitar/snare onset (drives mid-role lights)
    mid_strength: float = 0.0


# SuperFlux parameters, shared with the offline track-map analysis.
LOG_COMPRESSION = 10.0  # gamma in log1p(gamma * |X|)
MAX_FILTER_RADIUS = 3  # +-filterbank bands of max-filtering (vibrato guard)
ONSET_BANDS = 36  # log-spaced filterbank bands for the onset function
ONSET_FMIN = 40.0
ONSET_FMAX = 11000.0
BASS_ONSET_HZ = 200.0  # onsets below this drive the visible beat stream
MID_ONSET_HZ = 2500.0  # bass..this = the mid (guitar/snare) onset stream
# Absolute flux floor: the adaptive mean+k*std threshold self-normalises, so on
# near-silent onset streams (sustained tones, vibrato residue) it would shrink
# until numeric wobble "beats". Any real musical onset clears this easily.
MIN_ONSET_FLUX = 2.5
# A bass onset must also carry a real share of the total *linear* flux. In log
# domain a hi-hat's leakage into the low bands looks deceptively large (log1p
# inflates small values); in linear magnitude it is negligible (~0.003 of the
# attack) while a kick's flux is mostly bass — a clean discriminator.
BASS_FLUX_SHARE = 0.25
# Same idea for the guitar/snare stream: real mid-range energy, not a kick's
# attack splash (kick frames are bass-dominant and are excluded separately).
MID_FLUX_SHARE = 0.30
# A mid onset must also be a *percussive* attack. The discriminator is attack
# duration on the mid-band linear energy envelope: an instant transient (strum,
# snare) reaches its peak as fast as the 46 ms analysis window can fill
# (~3 hops), while a sung vowel swells over 80-150 ms (5+ hops) — which is why
# mid-role lights used to pop on syllables. The event fires on the first
# non-rising frame (one hop after the peak), trading ~20 ms of latency for
# never confusing a swell with a hit.
MID_MAX_ATTACK_FRAMES = 3
MID_RISE_RATIO = 1.05  # energy growth per frame that still counts as "rising"


def log_spectrum(mag: np.ndarray) -> np.ndarray:
    """Log-compressed magnitudes (perceptual flux scaling)."""
    return np.log1p(LOG_COMPRESSION * mag)


def max_filter_freq(logmag: np.ndarray, radius: int = MAX_FILTER_RADIUS) -> np.ndarray:
    """Maximum filter across ±``radius`` bands (along the last axis).

    The SuperFlux trick: comparing the current frame against a frequency-blurred
    previous frame means a tone wobbling by a band (vibrato, slides, leakage
    skirts) produces no positive flux, while a genuine new onset still does.
    """
    out = logmag.copy()
    for s in range(1, radius + 1):
        out[..., :-s] = np.maximum(out[..., :-s], logmag[..., s:])
        out[..., s:] = np.maximum(out[..., s:], logmag[..., :-s])
    return out


def make_onset_filterbank(
    freqs: np.ndarray,
    n_bands: int = ONSET_BANDS,
    fmin: float = ONSET_FMIN,
    fmax: float = ONSET_FMAX,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Log-spaced band aggregation for the onset function.

    SuperFlux runs on a coarse filterbank rather than raw FFT bins: a single
    tone's leakage skirts wobble individual bins frame-to-frame, but its *band*
    energy stays put. Returns ``(start_indices, counts, n_bass, n_mid)`` where
    bands ``[0, n_bass)`` lie below :data:`BASS_ONSET_HZ` (the kick stream) and
    bands ``[n_bass, n_mid)`` lie below :data:`MID_ONSET_HZ` (guitar/snare).
    """
    fmax = min(fmax, float(freqs[-1]))
    edges = np.geomspace(fmin, fmax, n_bands + 1)
    idx = np.searchsorted(freqs, edges).astype(np.int64)
    # Ensure every band spans at least one bin (low bands are narrower than one).
    for i in range(1, len(idx)):
        idx[i] = max(idx[i], idx[i - 1] + 1)
    idx = np.minimum(idx, len(freqs) - 1)
    starts = idx[:-1]
    counts = np.maximum(1, idx[1:] - idx[:-1])
    n_bass = max(1, int(np.sum(edges[1:] <= BASS_ONSET_HZ)))
    n_mid = max(n_bass + 1, int(np.sum(edges[1:] <= MID_ONSET_HZ)))
    return starts, counts, n_bass, n_mid


def band_means(mag: np.ndarray, starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Aggregate a magnitude spectrum into linear filterbank band means."""
    sums = np.add.reduceat(mag, starts, axis=-1)
    return sums / counts


def onset_bands(mag: np.ndarray, starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Aggregate a magnitude spectrum into log-compressed filterbank bands."""
    return log_spectrum(band_means(mag, starts, counts))


class _AGC:
    """Per-band automatic gain control: tracks a decaying peak as the 1.0 ref."""

    __slots__ = ("_peak", "_decay", "_floor")

    def __init__(self, decay: float = 0.9985, floor: float = 1e-6) -> None:
        self._peak = floor
        self._decay = decay
        self._floor = floor

    def normalise(self, value: float) -> float:
        self._peak = max(value, self._peak * self._decay, self._floor)
        return min(1.0, value / self._peak)

    def reset(self) -> None:
        """Forget the peak (rising is instant, so this is always safe)."""
        self._peak = self._floor


class Analyzer:
    """Turns a stream of audio hops into :class:`AnalysisFrame` features."""

    def __init__(
        self,
        sample_rate: int = ANALYSIS_SAMPLE_RATE,
        window: int = ANALYSIS_WINDOW,
        hop: int = ANALYSIS_HOP,
        beat_sensitivity: float = 1.2,
        noise_floor: float = ANALYSIS_NOISE_FLOOR,
    ) -> None:
        self._sr = sample_rate
        self._window = window
        self._hop = hop
        self._noise_floor = noise_floor
        self._buf = np.zeros(window, dtype=np.float32)
        self._hann = np.hanning(window).astype(np.float32)

        # Precompute FFT bin index ranges per band.
        freqs = np.fft.rfftfreq(window, 1.0 / sample_rate)
        self._freqs = freqs.astype(np.float32)
        self._band_bins: dict[str, tuple[int, int]] = {}
        for name, (lo, hi) in BANDS.items():
            lo_i = int(np.searchsorted(freqs, lo, side="left"))
            hi_i = int(np.searchsorted(freqs, hi, side="right"))
            self._band_bins[name] = (lo_i, max(lo_i + 1, hi_i))
        # Band/energy AGC decays slowly (~70 s half-life at 50 fps): it should
        # absorb mastering-level differences between tracks, NOT the dynamics
        # *within* one — with the old ~9 s half-life a quiet bridge re-inflated
        # to full brightness and the chorus arrived looking no louder.
        self._agc = {name: _AGC(decay=0.9998) for name in BANDS}
        self._energy_agc = _AGC(decay=0.9998)
        self._flux_agc = _AGC()
        self._bass_flux_agc = _AGC()
        self._mid_flux_agc = _AGC()

        # Onset / beat state (broadband for tempo; bass = kicks and mid =
        # guitar/snare for the visible accent streams).
        self._fb_starts, self._fb_counts, self._n_bass, self._n_mid = (
            make_onset_filterbank(freqs)
        )
        self._prev_log: np.ndarray | None = None
        self._prev_lin: np.ndarray | None = None
        # Mid-attack state machine (see MID_MAX_ATTACK_FRAMES).
        self._mid_e_prev = 0.0
        self._mid_rise_n = 0
        self._mid_attack_ok = False
        self._mid_attack_flux = 0.0
        self._flux_hist: deque[float] = deque(maxlen=43)  # ~0.9s at 50 fps
        self._bass_hist: deque[float] = deque(maxlen=43)
        self._mid_hist: deque[float] = deque(maxlen=43)
        self._sensitivity = beat_sensitivity
        self._refractory = 6  # min frames between beats (~120 ms)
        self._since_beat = self._refractory
        self._since_bass = self._refractory
        self._since_mid = self._refractory
        self._beat_times: deque[float] = deque(maxlen=8)
        self._frame_index = 0

    @property
    def frame_period(self) -> float:
        """Seconds represented by one hop/frame."""
        return self._hop / self._sr

    def push(self, hop: np.ndarray) -> AnalysisFrame:
        """Process one hop of mono float32 samples and return features."""
        if hop.dtype != np.float32:
            hop = hop.astype(np.float32)
        n = hop.shape[0]
        if n >= self._window:
            self._buf = hop[-self._window :].copy()
        else:
            self._buf = np.roll(self._buf, -n)
            self._buf[-n:] = hop

        rms = float(np.sqrt(np.mean(self._buf * self._buf)))
        if rms < self._noise_floor:
            # Master noise gate: the signal is effectively silent, so rest fully
            # instead of letting the per-band AGC amplify hiss/dither up to full.
            # Keep onset state coherent (no spurious beat on the next note) while
            # the AGC peaks decay on their own toward the floor.
            spectrum = np.fft.rfft(self._buf * self._hann)
            mag = np.abs(spectrum).astype(np.float32)
            self._prev_lin = band_means(mag, self._fb_starts, self._fb_counts)
            self._prev_log = log_spectrum(self._prev_lin)
            self._since_beat += 1
            self._since_bass += 1
            self._since_mid += 1
            # Keep the mid-attack machine coherent through silence.
            self._mid_rise_n = 0
            self._mid_e_prev = float(
                np.sum(self._prev_lin[self._n_bass : self._n_mid])
            )
            t_audio = self._frame_index * self.frame_period
            self._frame_index += 1
            return AnalysisFrame(
                bands={name: 0.0 for name in self._band_bins}, t_audio=t_audio
            )

        spectrum = np.fft.rfft(self._buf * self._hann)
        mag = np.abs(spectrum).astype(np.float32)
        power = mag * mag

        bands: dict[str, float] = {}
        for name, (lo_i, hi_i) in self._band_bins.items():
            raw = float(np.mean(power[lo_i:hi_i])) if hi_i > lo_i else 0.0
            bands[name] = self._agc[name].normalise(np.sqrt(raw))

        energy = self._energy_agc.normalise(rms)

        onsets = self._detect_onsets(mag)
        flux = self._flux_agc.normalise(onsets["flux"])
        bass_flux = self._bass_flux_agc.normalise(onsets["bass_flux"])
        centroid = self._spectral_centroid(mag)
        tempo = self._estimate_tempo()
        t_audio = self._frame_index * self.frame_period
        self._frame_index += 1

        return AnalysisFrame(
            bands=bands,
            energy=energy,
            beat=onsets["beat"],
            beat_strength=onsets["strength"],
            tempo_bpm=tempo,
            flux=flux,
            t_audio=t_audio,
            centroid=centroid,
            bass_flux=bass_flux,
            bass_beat=onsets["bass_beat"],
            bass_strength=onsets["bass_strength"],
            mid_flux=self._mid_flux_agc.normalise(onsets["mid_flux"]),
            mid_beat=onsets["mid_beat"],
            mid_strength=onsets["mid_strength"],
        )

    def _spectral_centroid(self, mag: np.ndarray) -> float:
        """Centre-of-mass frequency, normalised 0..1 (soft-capped at ~5 kHz).

        A perceptual "brightness" of the sound: rises through builds/risers and
        treble-heavy passages, falls on bass-heavy or muffled ones. Used by the
        structure tracker to spot tension/builds.
        """
        total = float(mag.sum())
        if total <= 1e-9:
            return 0.0
        centroid_hz = float((self._freqs * mag).sum()) / total
        return min(1.0, centroid_hz / 5000.0)

    def _detect_onsets(self, mag: np.ndarray) -> dict:
        """SuperFlux onsets on three streams: broadband (tempo), bass (kicks)
        and mid (guitar/snare). Returned as a dict of per-stream results."""
        lin = band_means(mag, self._fb_starts, self._fb_counts)
        cur = log_spectrum(lin)
        if self._prev_log is None or self._prev_lin is None:
            self._prev_log = cur
            self._prev_lin = lin
            return {
                "beat": False, "strength": 0.0, "flux": 0.0,
                "bass_beat": False, "bass_strength": 0.0, "bass_flux": 0.0,
                "mid_beat": False, "mid_strength": 0.0, "mid_flux": 0.0,
            }
        # SuperFlux on all three streams: positive increases over the
        # frequency-max-filtered previous frame. Vibrato, slides AND a low
        # tone's leakage-skirt wobble (a decaying kick tail "breathing" inside
        # the analysis window) produce none; real onsets do. The band streams
        # used plain flux before, which let tail wobble re-fire as fake beats.
        mf_prev = max_filter_freq(self._prev_log)
        diff = cur - mf_prev
        flux = float(np.sum(diff[diff > 0]))
        nb, nm = self._n_bass, self._n_mid
        bdiff = diff[:nb]
        bass_flux = float(np.sum(bdiff[bdiff > 0]))
        mdiff = diff[nb:nm]
        mid_flux = float(np.sum(mdiff[mdiff > 0]))
        # Linear-domain band shares of this frame's onset energy (leakage-proof:
        # log compression makes a hi-hat's splash into other bands look big).
        ldiff = lin - self._prev_lin
        lin_pos = float(np.sum(ldiff[ldiff > 0]))
        lb = ldiff[:nb]
        lm = ldiff[nb:nm]
        lin_bass = float(np.sum(lb[lb > 0]))
        lin_mid = float(np.sum(lm[lm > 0]))
        bass_share = lin_bass / lin_pos if lin_pos > 1e-9 else 0.0
        mid_share = lin_mid / lin_pos if lin_pos > 1e-9 else 0.0
        self._prev_log = cur
        self._prev_lin = lin

        beat, strength, self._since_beat = self._threshold_onset(
            flux, self._flux_hist, self._since_beat
        )
        if beat:
            self._beat_times.append(self._frame_index * self.frame_period)
        # Each stream requires both a real share of the onset's linear energy
        # AND dominance over the other: a kick is bass-dominant, a guitar pluck
        # or snare is mid-dominant — attack splash alone can't cross over.
        if bass_share >= BASS_FLUX_SHARE and bass_share >= mid_share:
            bass_beat, bass_strength, self._since_bass = self._threshold_onset(
                bass_flux, self._bass_hist, self._since_bass
            )
        else:  # broadband/treble splash (hi-hat attack), not a bass hit
            bass_beat, bass_strength = False, 0.0
            self._since_bass += 1
        # Mid stream: a small state machine over the mid-band linear energy.
        # Accumulate the rise (tracking its strongest mid-dominant flux), then
        # fire once when the rise completes — only if it completed fast enough
        # to be percussive. Swells never fire, and one strum fires exactly once.
        mid_beat, mid_strength = False, 0.0
        e_mid = float(np.sum(lin[nb:nm]))
        rising = e_mid > self._mid_e_prev * MID_RISE_RATIO
        if rising:
            if self._mid_rise_n == 0:
                self._mid_attack_flux = 0.0
                self._mid_attack_ok = False
            self._mid_rise_n += 1
            if mid_share >= MID_FLUX_SHARE and mid_share > bass_share:
                self._mid_attack_ok = True
                self._mid_attack_flux = max(self._mid_attack_flux, mid_flux)
            self._since_mid += 1
        else:
            if 1 <= self._mid_rise_n <= MID_MAX_ATTACK_FRAMES and self._mid_attack_ok:
                mid_beat, mid_strength, self._since_mid = self._threshold_onset(
                    self._mid_attack_flux,
                    self._mid_hist,
                    self._since_mid,
                    require_rising=False,
                )
            else:
                self._since_mid += 1
            self._mid_rise_n = 0
        self._mid_e_prev = e_mid

        self._flux_hist.append(flux)
        self._bass_hist.append(bass_flux)
        self._mid_hist.append(mid_flux)
        return {
            "beat": beat, "strength": strength, "flux": flux,
            "bass_beat": bass_beat, "bass_strength": bass_strength,
            "bass_flux": bass_flux,
            "mid_beat": mid_beat, "mid_strength": mid_strength,
            "mid_flux": mid_flux,
        }

    def _threshold_onset(
        self, flux: float, hist: deque[float], since: int, require_rising: bool = True
    ) -> tuple[bool, float, int]:
        """Adaptive median+k·MAD threshold with a refractory, on one flux stream.

        Median+MAD rather than mean+std: in dense, loud passages (wall-of-sound
        choruses) the sustained content drives both the mean and the deviation
        up until mean+k·std sits *above* the kicks and detection goes silent in
        the loudest part of the song. The median tracks the inter-onset
        baseline and MAD ignores the spikes, so kicks keep clearing it.
        """
        since += 1
        if len(hist) < hist.maxlen // 2:
            return False, 0.0, since
        arr = np.fromiter(hist, dtype=np.float32)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        threshold = max(med + self._sensitivity * 3.0 * mad, MIN_ONSET_FLUX)
        # Rising edge required (hist[-1] is the previous frame's flux): a real
        # attack is climbing when it crosses; a long decay tail re-crossing the
        # now-lower robust threshold right as the refractory expires is not.
        # (The mid stream skips this — its attack state machine fires one frame
        # *after* the energy peak by design, having verified the shape itself.)
        rising = (not require_rising) or flux > float(hist[-1])
        if flux > threshold and rising and since >= self._refractory:
            return True, min(3.0, flux / threshold), 0
        return False, 0.0, since

    def _estimate_tempo(self) -> float | None:
        if len(self._beat_times) < 4:
            return None
        intervals = np.diff(np.fromiter(self._beat_times, dtype=np.float64))
        intervals = intervals[(intervals > 0.25) & (intervals < 2.0)]  # 30-240 BPM
        if intervals.size < 2:
            return None
        return float(60.0 / np.median(intervals))

    def reset(self) -> None:
        """Clear transient state, e.g. on a track change."""
        self._buf[:] = 0.0
        # The band/energy AGC decays over ~70 s now (to keep verse/chorus
        # contrast within a track), so the peaks MUST reset between tracks —
        # otherwise a quiet song after a loud one renders dim for a minute.
        for agc in self._agc.values():
            agc.reset()
        self._energy_agc.reset()
        self._prev_log = None
        self._prev_lin = None
        self._mid_e_prev = 0.0
        self._mid_rise_n = 0
        self._mid_attack_ok = False
        self._mid_attack_flux = 0.0
        self._flux_hist.clear()
        self._bass_hist.clear()
        self._mid_hist.clear()
        self._beat_times.clear()
        self._since_beat = self._refractory
        self._since_bass = self._refractory
        self._since_mid = self._refractory
