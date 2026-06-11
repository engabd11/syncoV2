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


# SuperFlux parameters, shared with the offline track-map analysis.
LOG_COMPRESSION = 10.0  # gamma in log1p(gamma * |X|)
MAX_FILTER_RADIUS = 3  # +-filterbank bands of max-filtering (vibrato guard)
ONSET_BANDS = 36  # log-spaced filterbank bands for the onset function
ONSET_FMIN = 40.0
ONSET_FMAX = 11000.0
BASS_ONSET_HZ = 200.0  # onsets below this drive the visible beat stream
# Absolute flux floor: the adaptive mean+k*std threshold self-normalises, so on
# near-silent onset streams (sustained tones, vibrato residue) it would shrink
# until numeric wobble "beats". Any real musical onset clears this easily.
MIN_ONSET_FLUX = 2.5
# A bass onset must also carry a real share of the total *linear* flux. In log
# domain a hi-hat's leakage into the low bands looks deceptively large (log1p
# inflates small values); in linear magnitude it is negligible (~0.003 of the
# attack) while a kick's flux is mostly bass — a clean discriminator.
BASS_FLUX_SHARE = 0.25


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
) -> tuple[np.ndarray, np.ndarray, int]:
    """Log-spaced band aggregation for the onset function.

    SuperFlux runs on a coarse filterbank rather than raw FFT bins: a single
    tone's leakage skirts wobble individual bins frame-to-frame, but its *band*
    energy stays put. Returns ``(start_indices, counts, n_bass_bands)`` where the
    first ``n_bass_bands`` bands lie below :data:`BASS_ONSET_HZ`.
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
    n_bass = int(np.sum(edges[1:] <= BASS_ONSET_HZ))
    return starts, counts, max(1, n_bass)


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
        self._agc = {name: _AGC() for name in BANDS}
        self._energy_agc = _AGC()
        self._flux_agc = _AGC()
        self._bass_flux_agc = _AGC()

        # Onset / beat state (broadband for tempo, bass for visible accents).
        self._fb_starts, self._fb_counts, self._n_bass = make_onset_filterbank(freqs)
        self._prev_log: np.ndarray | None = None
        self._prev_lin: np.ndarray | None = None
        self._flux_hist: deque[float] = deque(maxlen=43)  # ~0.9s at 50 fps
        self._bass_hist: deque[float] = deque(maxlen=43)
        self._sensitivity = beat_sensitivity
        self._refractory = 6  # min frames between beats (~120 ms)
        self._since_beat = self._refractory
        self._since_bass = self._refractory
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

        beat, strength, flux_raw, bass_beat, bass_strength, bass_raw = (
            self._detect_onsets(mag)
        )
        flux = self._flux_agc.normalise(flux_raw)
        bass_flux = self._bass_flux_agc.normalise(bass_raw)
        centroid = self._spectral_centroid(mag)
        tempo = self._estimate_tempo()
        t_audio = self._frame_index * self.frame_period
        self._frame_index += 1

        return AnalysisFrame(
            bands=bands,
            energy=energy,
            beat=beat,
            beat_strength=strength,
            tempo_bpm=tempo,
            flux=flux,
            t_audio=t_audio,
            centroid=centroid,
            bass_flux=bass_flux,
            bass_beat=bass_beat,
            bass_strength=bass_strength,
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

    def _detect_onsets(
        self, mag: np.ndarray
    ) -> tuple[bool, float, float, bool, float, float]:
        """SuperFlux onsets: (beat, strength, flux, bass_beat, bass_strength, bass_flux)."""
        lin = band_means(mag, self._fb_starts, self._fb_counts)
        cur = log_spectrum(lin)
        if self._prev_log is None or self._prev_lin is None:
            self._prev_log = cur
            self._prev_lin = lin
            return False, 0.0, 0.0, False, 0.0, 0.0
        # Broadband SuperFlux: positive increases over the frequency-max-filtered
        # previous frame (vibrato/slides produce none; real onsets do).
        diff = cur - max_filter_freq(self._prev_log)
        flux = float(np.sum(diff[diff > 0]))
        # Bass flux: plain positive log-flux restricted to the kick/bass bands.
        bdiff = cur[: self._n_bass] - self._prev_log[: self._n_bass]
        bass_flux = float(np.sum(bdiff[bdiff > 0]))
        # Linear-domain bass share of this frame's onset energy (leakage-proof).
        ldiff = lin - self._prev_lin
        lin_pos = float(np.sum(ldiff[ldiff > 0]))
        lb = ldiff[: self._n_bass]
        lin_bass = float(np.sum(lb[lb > 0]))
        bass_share = lin_bass / lin_pos if lin_pos > 1e-9 else 0.0
        self._prev_log = cur
        self._prev_lin = lin

        beat, strength, self._since_beat = self._threshold_onset(
            flux, self._flux_hist, self._since_beat
        )
        if beat:
            self._beat_times.append(self._frame_index * self.frame_period)
        if bass_share >= BASS_FLUX_SHARE:
            bass_beat, bass_strength, self._since_bass = self._threshold_onset(
                bass_flux, self._bass_hist, self._since_bass
            )
        else:  # broadband/treble splash (hi-hat attack), not a bass hit
            bass_beat, bass_strength = False, 0.0
            self._since_bass += 1

        self._flux_hist.append(flux)
        self._bass_hist.append(bass_flux)
        return beat, strength, flux, bass_beat, bass_strength, bass_flux

    def _threshold_onset(
        self, flux: float, hist: deque[float], since: int
    ) -> tuple[bool, float, int]:
        """Adaptive mean+k·std threshold with a refractory, on one flux stream."""
        since += 1
        if len(hist) < hist.maxlen // 2:
            return False, 0.0, since
        arr = np.fromiter(hist, dtype=np.float32)
        threshold = max(
            float(arr.mean() + self._sensitivity * arr.std()), MIN_ONSET_FLUX
        )
        if flux > threshold and since >= self._refractory:
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
        self._prev_log = None
        self._prev_lin = None
        self._flux_hist.clear()
        self._bass_hist.clear()
        self._beat_times.clear()
        self._since_beat = self._refractory
        self._since_bass = self._refractory
