"""Offline track map: the whole song analysed ahead of time.

The reference for "perfect" light sync (the Hue×Spotify integration) does no
real-time detection at all — it reads a precomputed per-track analysis
(timestamped beats, sections, loudness) and *schedules* the show against the
playback position, anticipating every beat. This module computes that analysis
ourselves from the track's audio, with ffmpeg + numpy only:

* **Onset envelope** — the same SuperFlux filterbank flux as the live
  :mod:`analyzer`, vectorised over the whole file (streamed, so memory stays
  small even for long tracks).
* **Global tempo** — autocorrelation of the envelope with a log-Gaussian prior
  around 120 BPM (as in :mod:`tempo`), but over the *whole* track.
* **Beats** — the Ellis dynamic-programming beat tracker: globally optimal beat
  times balancing onset strength against tempo regularity. Unlike any causal
  tracker it cannot be fooled by a fill or a quiet bar, because it sees the
  future.
* **Downbeats** — the 4-beat fold with the strongest bass accent.
* **Sections** — a novelty curve (checkerboard kernel over a self-similarity
  matrix of band-energy block features) splits the track into sections, each
  labelled with its relative energy so the show can save its fireworks for the
  chorus.

A :class:`TrackMap` then answers ``grid_at(position)`` with an authoritative
:class:`~.tempo.BeatGrid` for the playback position — confidence 1:1 with the
analysis, exact time-to-next-beat, real downbeats.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..color.palette import RGB
from ..color.song_palette import palette_from_chroma
from ..const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    ANALYSIS_WINDOW,
    BANDS,
    MELBANK_BINS,
)
from .analyzer import (
    AnalysisFrame,
    band_means,
    log_spectrum,
    make_melbank,
    make_onset_filterbank,
    max_filter_freq,
)
from .filters import ExpFilter
from .tempo import BeatGrid

_LOGGER = logging.getLogger(__name__)

_FRAME_PERIOD = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE

# Tempo prior (matches the live tracker's range/centre).
_MIN_BPM = 70.0
_MAX_BPM = 190.0
_PRIOR_CENTER_BPM = 120.0
_PRIOR_WIDTH = 0.55

# Ellis DP beat tracker: how strongly deviations from the tempo period are
# punished relative to onset strength (librosa uses 100 on a similar scale).
_TIGHTNESS = 100.0

# Section segmentation.
_BLOCK_S = 0.75  # block size for self-similarity features
_KERNEL_BLOCKS = 8  # checkerboard kernel half-size (~6 s context)
_MIN_SECTION_S = 8.0

# Use a track map only when the analysis was confident enough to trust.
MIN_MAP_CONFIDENCE = 0.30

# On-disk cache format version: bump when the serialised layout changes so stale
# files are ignored rather than mis-read. Stored in each .npz's ``meta`` row.
_CACHE_FORMAT = 1

_MAX_TRACK_S = 720.0  # analysis cap (12 min); beyond this fall back to live
_DECODE_TIMEOUT_S = 90.0  # ffmpeg must finish well before the track does

# Audio we must decode before treating a result as "the analysis genuinely ran"
# rather than "the fetch failed" — below this, a None result is a transient
# decode/network failure (e.g. Navidrome busy) and is worth retrying.
_MIN_DECODE_S = 5.0
# Retry policy for *transient* analysis failures (decode/timeout): a busy
# library shouldn't strand a track for the whole session. Exhausting these (or a
# decoded-but-unusable result, which won't improve) marks the track failed.
_MAX_ANALYSIS_ATTEMPTS = 4
_RETRY_BASE_S = 15.0
_RETRY_MAX_S = 120.0


@dataclass(slots=True)
class MapResult:
    """Outcome of one analysis attempt.

    ``decoded`` distinguishes "we got the audio but couldn't build a usable map"
    (permanent — re-running won't help) from "we couldn't get the audio"
    (transient — retry), so a slow/busy Navidrome doesn't permanently disable a
    track. ``error`` carries the ffmpeg diagnostic for the log.
    """

    track_map: "TrackMap | None"
    decoded: bool
    error: str = ""


@dataclass(slots=True)
class Section:
    start: float
    end: float
    energy: float  # 0..1 loudness of this section relative to the track's peak
    # Colours derived from this section's harmony (chroma -> hue), used by the
    # SONG colour scheme. Empty when the section had no usable pitch content.
    palette: list[RGB] = field(default_factory=list)


@dataclass(slots=True)
class TrackFeatures:
    """Per-frame audio features, globally normalised, ready to *play back*.

    This is what makes the integration universal: a player we cannot tap live
    (AirPlay, Chromecast, Sonos, DLNA, ...) can still get a fully beat-accurate
    show by replaying these precomputed frames against its playback position.
    Global normalisation (95th percentile = 1.0) replaces the live AGC — and is
    better, because it sees the whole track at once. ~400 KB per track.
    """

    bands: np.ndarray  # (n_frames, len(BANDS)) float32, 0..1
    energy: np.ndarray  # (n_frames,) float32, 0..1
    flux: np.ndarray  # (n_frames,) float32, 0..~1 (tempo-model food)
    bass_flux: np.ndarray  # (n_frames,) float32, 0..~1
    mid_flux: np.ndarray  # (n_frames,) float32, 0..~1 (guitar/snare)
    centroid: np.ndarray  # (n_frames,) float32, 0..1 (structure brightness)
    # Precomputed LedFx-style melbank (n_frames, MELBANK_BINS), gain-normalised
    # and smoothed offline so scheduled playback drives the same continuous
    # reactive layer as the live tap. Empty array on older/failed analyses.
    melbank: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=np.float32))


@dataclass(slots=True)
class TrackMap:
    """Precomputed beat/section schedule for one track."""

    duration: float
    bpm: float
    confidence: float  # 0..1 tempo-lock quality of the offline analysis
    beats: np.ndarray  # beat timestamps, seconds
    accents: np.ndarray  # onset strength at each beat, 0..1
    downbeat: int  # index into ``beats`` of the first bar start
    sections: list[Section] = field(default_factory=list)
    features: TrackFeatures | None = None  # per-frame playback features
    # Guitar/snare onsets (timestamps + 0..1 accents) for the mid-role lights.
    mid_beats: np.ndarray = field(default_factory=lambda: np.zeros(0))
    mid_accents: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def usable(self) -> bool:
        return self.confidence >= MIN_MAP_CONFIDENCE and self.beats.size >= 8

    def save(self, path: str | Path) -> None:
        """Serialise to a compact ``.npz`` (no pickle) for the persistent cache.

        Numeric arrays are stored directly; sections (including their SONG
        palettes) are flattened into parallel arrays and rebuilt on load. The
        whole map is ~0.2-0.4 MB compressed, so a played track can be cached and
        replayed instantly next time, and a library can be pre-analysed ahead.
        """
        f = self.features
        pal_rgb = np.array(
            [c for s in self.sections for c in s.palette], dtype=np.float32
        ).reshape(-1, 3)
        pal_counts = np.array([len(s.palette) for s in self.sections], dtype=np.int32)
        data = {
            "meta": np.array(
                [self.duration, self.bpm, self.confidence, float(self.downbeat),
                 float(_CACHE_FORMAT)], dtype=np.float64),
            "beats": self.beats.astype(np.float64),
            "accents": self.accents.astype(np.float32),
            "mid_beats": self.mid_beats.astype(np.float64),
            "mid_accents": self.mid_accents.astype(np.float32),
            "sec_start": np.array([s.start for s in self.sections], dtype=np.float64),
            "sec_end": np.array([s.end for s in self.sections], dtype=np.float64),
            "sec_energy": np.array([s.energy for s in self.sections], dtype=np.float32),
            "pal_rgb": pal_rgb,
            "pal_counts": pal_counts,
        }
        if f is not None:
            data.update({
                "f_bands": f.bands, "f_energy": f.energy, "f_flux": f.flux,
                "f_bass_flux": f.bass_flux, "f_mid_flux": f.mid_flux,
                "f_centroid": f.centroid, "f_melbank": f.melbank,
            })
        tmp = Path(path).with_suffix(".npz.tmp")
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, **data)
        tmp.replace(path)  # atomic: a reader never sees a half-written file

    @classmethod
    def load(cls, path: str | Path) -> "TrackMap | None":
        """Rebuild a :class:`TrackMap` from :meth:`save`; None if unreadable/stale."""
        try:
            with np.load(path, allow_pickle=False) as z:
                meta = z["meta"]
                if int(round(float(meta[4]))) != _CACHE_FORMAT:
                    return None
                pal_rgb = z["pal_rgb"]
                pal_counts = z["pal_counts"]
                sections: list[Section] = []
                k = 0
                for i in range(len(z["sec_start"])):
                    n = int(pal_counts[i]) if i < len(pal_counts) else 0
                    palette = [tuple(map(float, rgb)) for rgb in pal_rgb[k:k + n]]
                    k += n
                    sections.append(Section(
                        float(z["sec_start"][i]), float(z["sec_end"][i]),
                        float(z["sec_energy"][i]), palette))
                features = None
                if "f_energy" in z:
                    features = TrackFeatures(
                        bands=z["f_bands"], energy=z["f_energy"], flux=z["f_flux"],
                        bass_flux=z["f_bass_flux"], mid_flux=z["f_mid_flux"],
                        centroid=z["f_centroid"], melbank=z["f_melbank"])
                return cls(
                    duration=float(meta[0]), bpm=float(meta[1]),
                    confidence=float(meta[2]), beats=z["beats"],
                    accents=z["accents"], downbeat=int(round(float(meta[3]))),
                    sections=sections, features=features,
                    mid_beats=z["mid_beats"], mid_accents=z["mid_accents"])
        except (OSError, KeyError, ValueError, IndexError):
            return None

    def frame_at(self, pos: float, prev_pos: float | None = None) -> AnalysisFrame | None:
        """Synthesise the :class:`AnalysisFrame` for playback position ``pos``.

        ``prev_pos`` bounds the beat-window so each scheduled beat fires exactly
        once as the position advances. Returns None outside the analysed span
        (callers fall back to their own behaviour).
        """
        f = self.features
        if f is None:
            return None
        i = int(pos / _FRAME_PERIOD)
        if i < 0 or i >= f.energy.shape[0]:
            return None
        # A beat fires when one of the scheduled beat times falls inside
        # (prev_pos, pos] — exactly once per beat as the clock sweeps past it.
        lo = prev_pos if prev_pos is not None and prev_pos < pos else pos - _FRAME_PERIOD
        j0 = int(np.searchsorted(self.beats, lo, side="right"))
        j1 = int(np.searchsorted(self.beats, pos, side="right"))
        beat = j1 > j0
        # Accent-weighted strength on the live detector's ~1..3 scale, so the
        # per-mode beat_threshold gates behave exactly like the live path.
        strength = 1.0 + 2.0 * float(self.accents[j1 - 1]) if beat else 0.0
        # Scheduled guitar/snare onsets for the mid-role lights.
        m0 = int(np.searchsorted(self.mid_beats, lo, side="right"))
        m1 = int(np.searchsorted(self.mid_beats, pos, side="right"))
        mid = m1 > m0
        mid_strength = 1.0 + 2.0 * float(self.mid_accents[m1 - 1]) if mid else 0.0
        bands = {name: float(v) for name, v in zip(BANDS, f.bands[i])}
        melbank = f.melbank[i].tolist() if f.melbank.shape[0] > i else []
        return AnalysisFrame(
            bands=bands,
            energy=float(f.energy[i]),
            beat=beat,
            beat_strength=strength,
            tempo_bpm=self.bpm,
            flux=float(f.flux[i]),
            t_audio=pos,
            centroid=float(f.centroid[i]),
            bass_flux=float(f.bass_flux[i]),
            bass_beat=beat,
            bass_strength=strength,
            mid_flux=float(f.mid_flux[i]),
            mid_beat=mid,
            mid_strength=mid_strength,
            melbank=melbank,
        )

    def grid_at(self, pos: float, prev_pos: float | None = None) -> BeatGrid | None:
        """The authoritative beat grid at playback position ``pos`` (seconds).

        ``prev_pos`` (the previous query) makes ``predicted_beat`` fire exactly
        once per crossed beat. Returns None outside the analysed span.
        """
        beats = self.beats
        if beats.size < 2:
            return None
        if pos < beats[0] - 4.0 or pos > beats[-1] + 4.0:
            return None
        i = int(np.searchsorted(beats, pos, side="right"))  # beats[i-1] <= pos
        if i <= 0:
            period = float(beats[1] - beats[0])
            prev_b = beats[0] - period
            next_b = float(beats[0])
            idx_prev = -1
        elif i >= beats.size:
            period = float(beats[-1] - beats[-2])
            prev_b = float(beats[-1])
            next_b = prev_b + period
            idx_prev = beats.size - 1
        else:
            prev_b = float(beats[i - 1])
            next_b = float(beats[i])
            period = next_b - prev_b
            idx_prev = i - 1
        period = max(1e-3, period)
        phase = min(1.0, max(0.0, (pos - prev_b) / period))
        crossed = (
            prev_pos is not None
            and prev_pos < pos
            and int(np.searchsorted(beats, prev_pos, side="right")) != i
        )
        beat_idx = (idx_prev - self.downbeat) % 4
        # Accent of the upcoming beat (anticipatory waves) and of the beat that
        # just started (at-beat flashes) — the Hue+Spotify "knows the future"
        # feel, exact per beat because the whole track was analysed.
        next_idx = min(self.accents.size - 1, max(0, idx_prev + 1))
        accent = float(self.accents[next_idx]) if self.accents.size else 1.0
        now_idx = min(self.accents.size - 1, max(0, idx_prev))
        accent_now = float(self.accents[now_idx]) if self.accents.size else 1.0
        return BeatGrid(
            bpm=60.0 / period,
            confidence=self.confidence,
            locked=True,
            period_s=period,
            phase=phase,
            time_to_next_beat=max(0.0, next_b - pos),
            next_beat_t=next_b,
            bar_phase=(beat_idx + phase) / 4.0,
            predicted_beat=bool(crossed),
            accent=accent,
            accent_now=accent_now,
            beat_in_bar=beat_idx,
        )

    def section_at(self, pos: float) -> Section | None:
        for s in self.sections:
            if s.start <= pos < s.end:
                return s
        return None

    def next_boundary(self, pos: float) -> tuple[float, float] | None:
        """(seconds-until, next-section energy) of the upcoming boundary."""
        for s in self.sections:
            if s.start > pos:
                return s.start - pos, s.energy
        return None


# Chroma (pitch-class) range: fundamentals from ~A1 to ~D8. Below/above this,
# bins are mostly hum, percussion noise or harmonics that muddy the key.
_CHROMA_FMIN = 55.0
_CHROMA_FMAX = 5000.0


def _chroma_projection(freqs: np.ndarray) -> np.ndarray:
    """``(n_bins, 12)`` matrix folding FFT bins into pitch classes (0=C..11=B)."""
    proj = np.zeros((freqs.size, 12), dtype=np.float32)
    for b in range(freqs.size):
        f = float(freqs[b])
        if _CHROMA_FMIN <= f <= _CHROMA_FMAX:
            midi = 69.0 + 12.0 * np.log2(f / 440.0)
            proj[b, int(round(midi)) % 12] = 1.0
    return proj


class EnvelopeExtractor:
    """Streamed STFT feature extraction (constant memory, vectorised per chunk).

    Feed arbitrary-length float32 chunks with :meth:`push`; collect the
    per-frame onset envelope, bass envelope, RMS and block band profiles.
    """

    def __init__(
        self,
        sample_rate: int = ANALYSIS_SAMPLE_RATE,
        window: int = ANALYSIS_WINDOW,
        hop: int = ANALYSIS_HOP,
    ) -> None:
        self._window = window
        self._hop = hop
        self._hann = np.hanning(window).astype(np.float32)
        freqs = np.fft.rfftfreq(window, 1.0 / sample_rate)
        self._fb_starts, self._fb_counts, self.n_bass, self.n_mid = (
            make_onset_filterbank(freqs.astype(np.float32))
        )
        # Continuous melbank (same filterbank as the live analyzer) + chroma
        # projection, both precomputed once per analysis.
        self._mel_starts, self._mel_counts = make_melbank(freqs.astype(np.float32))
        self._chroma_proj = _chroma_projection(freqs)
        self.melbank: list[np.ndarray] = []  # per-frame melbank means
        self.chroma: list[np.ndarray] = []  # per-frame 12-bin chromagram
        self._freqs = freqs.astype(np.float32)
        # Bin ranges of the named output bands (same edges as the live analyzer).
        self._named_bins = []
        for lo, hi in BANDS.values():
            lo_i = int(np.searchsorted(freqs, lo, side="left"))
            hi_i = int(np.searchsorted(freqs, hi, side="right"))
            self._named_bins.append((lo_i, max(lo_i + 1, hi_i)))
        self._tail = np.zeros(0, dtype=np.float32)
        self._prev_log: np.ndarray | None = None
        self._prev_lin: np.ndarray | None = None
        self.env: list[float] = []  # SuperFlux onset envelope per frame
        self.bass_env: list[float] = []  # bass-band log-flux per frame
        self.mid_env: list[float] = []  # mid-band (guitar/snare) log-flux
        # Per-frame linear-domain mid dominance (same gate as the live
        # detector): without it the offline mid stream fires on every kick's
        # attack splash, and map-driven "guitar" lights pop on the kicks.
        self.mid_dom: list[bool] = []
        self.rms: list[float] = []
        self.bands: list[np.ndarray] = []  # linear filterbank means per frame
        self.named_bands: list[np.ndarray] = []  # the 5 output bands per frame
        self.centroid: list[float] = []

    def push(self, samples: np.ndarray) -> None:
        buf = np.concatenate([self._tail, samples]) if self._tail.size else samples
        if buf.size < self._window:
            self._tail = buf
            return
        n_frames = 1 + (buf.size - self._window) // self._hop
        frames = np.lib.stride_tricks.sliding_window_view(buf, self._window)[
            :: self._hop
        ][:n_frames]
        windowed = frames * self._hann
        mags = np.abs(np.fft.rfft(windowed, axis=1)).astype(np.float32)
        lin = band_means(mags, self._fb_starts, self._fb_counts)
        logb = log_spectrum(lin)
        # Prepend the previous chunk's last frame so flux is continuous.
        if self._prev_log is not None:
            prev = np.vstack([self._prev_log[None, :], logb[:-1]])
        else:
            prev = np.vstack([logb[:1], logb[:-1]])
        # SuperFlux on all three streams (matches the live analyzer): the
        # max-filtered reference also kills a low tone's leakage-skirt wobble
        # in the band-limited kick/guitar envelopes, not just broadband.
        diff = logb - max_filter_freq(prev)
        np.maximum(diff, 0.0, out=diff)
        self.env.extend(np.sum(diff, axis=1).tolist())
        self.bass_env.extend(np.sum(diff[:, : self.n_bass], axis=1).tolist())
        self.mid_env.extend(np.sum(diff[:, self.n_bass : self.n_mid], axis=1).tolist())
        # Linear-domain onset shares (leakage-proof, as in the live analyzer).
        if self._prev_lin is not None:
            prev_lin = np.vstack([self._prev_lin[None, :], lin[:-1]])
        else:
            prev_lin = np.vstack([lin[:1], lin[:-1]])
        ldiff = np.maximum(lin - prev_lin, 0.0)
        lin_pos = np.maximum(ldiff.sum(axis=1), 1e-9)
        bass_share = ldiff[:, : self.n_bass].sum(axis=1) / lin_pos
        mid_share = ldiff[:, self.n_bass : self.n_mid].sum(axis=1) / lin_pos
        self.mid_dom.extend(((mid_share >= 0.30) & (mid_share > bass_share)).tolist())
        self._prev_lin = lin[-1]
        self.rms.extend(np.sqrt(np.mean(frames * frames, axis=1)).tolist())
        self.bands.extend(lin)
        # The 5 named output bands (same maths as the live analyzer: RMS of the
        # band's power) and the spectral centroid, per frame.
        power = mags * mags
        named = np.stack(
            [np.sqrt(np.mean(power[:, lo:hi], axis=1)) for lo, hi in self._named_bins],
            axis=1,
        )
        self.named_bands.extend(named)
        total = np.maximum(mags.sum(axis=1), 1e-9)
        cent = np.minimum(1.0, (mags @ self._freqs) / total / 5000.0)
        self.centroid.extend(cent.tolist())
        # Continuous melbank (drives the LedFx reactive layer on playback) and
        # the chromagram (drives the SONG colour scheme), per frame.
        self.melbank.extend(band_means(mags, self._mel_starts, self._mel_counts))
        self.chroma.extend(mags @ self._chroma_proj)
        self._prev_log = logb[-1]
        consumed = n_frames * self._hop
        self._tail = buf[consumed:].copy()


def _estimate_tempo(env: np.ndarray) -> tuple[float, float]:
    """Global (bpm, confidence) from the onset envelope autocorrelation."""
    x = env - env.mean()
    if not np.any(x):
        return 0.0, 0.0
    n = x.size
    ac = np.correlate(x, x, mode="full")[n - 1 :]
    zero = ac[0] if ac[0] > 1e-9 else 1e-9
    min_lag = max(1, int(round(60.0 / _MAX_BPM / _FRAME_PERIOD)))
    max_lag = min(n - 1, int(round(60.0 / _MIN_BPM / _FRAME_PERIOD)))
    if max_lag <= min_lag:
        return 0.0, 0.0
    lags = np.arange(min_lag, max_lag + 1)
    cand_bpm = 60.0 / (lags * _FRAME_PERIOD)
    prior = np.exp(-0.5 * (np.log(cand_bpm / _PRIOR_CENTER_BPM) / _PRIOR_WIDTH) ** 2)
    seg = ac[min_lag : max_lag + 1]
    best = int(np.argmax(seg * prior))
    # Octave-error guard: a true fundamental has autocorrelation energy at all
    # its multiples (a "harmonic comb"), while a double-tempo false peak
    # (eighth-notes mistaken for the beat) lacks the sub-multiple support. Only
    # consider switching the chosen period to its half/double relative, and only
    # when that relative's comb*prior actually wins — so clear cases are left
    # untouched and only genuine 2x/0.5x errors ("every other beat") are fixed.
    def _comb(idx: int) -> float:
        lag = int(lags[idx])
        s = float(seg[idx])
        for harm, weight in ((2, 0.5), (3, 0.34)):
            hl = lag * harm
            if hl <= n - 1:
                s += weight * float(ac[hl])
        return s * float(prior[idx])

    candidates = [best]
    for rel in (0.5, 2.0):
        ridx = int(round(int(lags[best]) * rel)) - min_lag
        if 0 <= ridx < seg.size:
            candidates.append(ridx)
    best = max(candidates, key=_comb)
    bpm = float(cand_bpm[best])
    confidence = float(max(0.0, min(1.0, seg[best] / zero)))
    return bpm, confidence


# Beats-on-peaks contrast below this reads as noise (measured: white noise
# ~2.9, real steady grooves 4.2-5.7); the rescue scales in over the next 1.6.
_RESCUE_CONTRAST_FLOOR = 3.4
_RESCUE_CONF_CAP = 0.60


def _rescue_confidence(
    conf: float,
    env: np.ndarray,
    beat_frames: np.ndarray,
    beats: np.ndarray,
    duration: float,
) -> float:
    """Lift a diluted tempo confidence using the tracked beat grid itself.

    Only when the grid looks like a real steady groove: enough beats, a
    musically-plausible and tight median interval, coverage of most of the
    track, and — the discriminator the DP tracker's regularising bias cannot
    fake — beats landing on onset-envelope peaks far above the track mean.
    Returns the (possibly lifted) confidence; never lowers it.
    """
    if beats.size < 16 or duration < 10.0:
        return conf
    intervals = np.diff(beats)
    med = float(np.median(intervals))
    if med <= 0 or not (60.0 / _MAX_BPM <= med <= 60.0 / _MIN_BPM):
        return conf
    mad = float(np.median(np.abs(intervals - med)))
    if mad / med > 0.12:
        return conf  # wobbly grid: not a steady groove
    if (beats[-1] - beats[0]) < 0.6 * duration:
        return conf  # beats cover too little of the track
    mean = float(env.mean())
    if mean <= 1e-9:
        return conf
    idx = np.minimum(beat_frames, env.size - 1)
    contrast = float(env[idx].mean()) / mean
    rescue = 0.55 * min(1.0, max(0.0, (contrast - _RESCUE_CONTRAST_FLOOR) / 1.6))
    if rescue <= 0.0:
        return conf
    return max(conf, min(_RESCUE_CONF_CAP, conf + rescue))


def _track_beats(env: np.ndarray, bpm: float) -> np.ndarray:
    """Ellis dynamic-programming beat tracker; returns beat frame indices.

    Maximises sum(onset strength at beats) − tightness·Σ log²(interval/period):
    the globally optimal beat sequence for the (constant) tempo estimate, with
    enough flex to ride small tempo drift.
    """
    if bpm <= 0 or env.size < 16:
        return np.zeros(0, dtype=np.int64)
    period = 60.0 / bpm / _FRAME_PERIOD  # frames per beat
    # Normalise the onset envelope to unit std so _TIGHTNESS is scale-free.
    std = env.std()
    local = env / std if std > 1e-9 else env
    n = local.size
    lo = -int(round(2.0 * period))
    hi = -max(1, int(round(period / 2.0)))
    prange = np.arange(lo, hi + 1)
    txwt = -_TIGHTNESS * (np.log(-prange / period) ** 2)
    cumscore = local.copy()
    backlink = np.full(n, -1, dtype=np.int64)
    first_beat = True
    for i in range(max(1, -lo), n):
        candidates = txwt + cumscore[i + prange]
        best = int(np.argmax(candidates))
        score = float(candidates[best])
        if first_beat:
            # Don't force a chain before the music starts.
            cumscore[i] = local[i] + max(0.0, score)
            if score > 0.0:
                backlink[i] = i + prange[best]
                first_beat = False
        else:
            cumscore[i] = local[i] + score
            backlink[i] = i + prange[best]
    # Backtrace from the best score in the final beat period.
    tail_start = max(0, n - int(round(period)))
    last = tail_start + int(np.argmax(cumscore[tail_start:]))
    beats = [last]
    while backlink[beats[-1]] >= 0:
        beats.append(int(backlink[beats[-1]]))
    return np.array(beats[::-1], dtype=np.int64)


def _pick_onsets(
    env: np.ndarray,
    sensitivity: float = 1.5,
    min_gap: int = 6,
    floor: float = 2.5,
    allowed: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Offline onset peak-picking with a rolling adaptive threshold.

    Returns ``(times, accents)`` — onset timestamps (s) and 0..1 accents — the
    scheduled equivalent of the live mid-onset detector. ``allowed`` is an
    optional per-frame eligibility mask (e.g. mid-dominance); it is dilated by
    one frame each way since a peak can land a hop off its dominant frame.
    """
    n = env.size
    if n < 64:
        return np.zeros(0), np.zeros(0)
    w = 43  # ~0.9 s, matching the live detector's history window
    kernel = np.ones(w) / w
    mean = np.convolve(env, kernel, mode="same")
    var = np.convolve(env * env, kernel, mode="same") - mean * mean
    thr = np.maximum(mean + sensitivity * np.sqrt(np.maximum(var, 0.0)), floor)
    is_peak = (env > thr) & (env >= np.roll(env, 1)) & (env >= np.roll(env, -1))
    if allowed is not None and allowed.size == n:
        ok = allowed | np.roll(allowed, 1) | np.roll(allowed, -1)
        is_peak &= ok
    cand = np.where(is_peak)[0]
    picked: list[int] = []
    last = -min_gap
    for i in cand:
        if i - last >= min_gap:
            picked.append(int(i))
            last = int(i)
    if not picked:
        return np.zeros(0), np.zeros(0)
    idx = np.array(picked, dtype=np.int64)
    strength = np.minimum(3.0, env[idx] / np.maximum(thr[idx], 1e-9))
    accents = np.clip((strength - 1.0) / 2.0, 0.0, 1.0)
    return idx.astype(np.float64) * _FRAME_PERIOD, accents


def _percussive_filter(
    times: np.ndarray,
    accents: np.ndarray,
    energy: np.ndarray,
    max_attack: int = 3,
    ratio: float = 1.05,
) -> tuple[np.ndarray, np.ndarray]:
    """Keep only onsets whose energy attack completed within ``max_attack`` hops.

    The offline twin of the live mid-attack state machine: a strum or snare
    reaches its energy peak as fast as the analysis window fills (~3 hops),
    while a sung vowel swells over 80-150 ms — those are the syllable "onsets"
    that made mid lights pop on the singing.
    """
    if times.size == 0:
        return times, accents
    keep = np.zeros(times.size, dtype=bool)
    n = energy.size
    for k, t in enumerate(times):
        i = int(round(t / _FRAME_PERIOD))
        if i >= n:
            continue
        peak = i + int(np.argmax(energy[i : min(n, i + 5)]))
        rise = 0
        j = peak
        while j > 0 and energy[j] > energy[j - 1] * ratio:
            rise += 1
            j -= 1
            if rise > max_attack + 2:
                break
        keep[k] = 1 <= rise <= max_attack
    return times[keep], accents[keep]


def _quantize_to_eighths(
    times: np.ndarray,
    accents: np.ndarray,
    beats: np.ndarray,
    tolerance: float = 0.12,
) -> tuple[np.ndarray, np.ndarray]:
    """Keep only onsets within ``tolerance`` beats of an eighth-note slot.

    Guitar riffs and snares live on the eighth grid (on-beats and off-beats —
    syncopation included); vocal syllables and ornaments float between slots.
    Filtering the scheduled mid onsets to the grid is the strongest "stop
    popping on the singing" guard the offline analysis can apply.
    """
    if times.size == 0 or beats.size < 2:
        return times, accents
    mids = beats[:-1] + np.diff(beats) / 2.0
    slots = np.sort(np.concatenate([beats, mids]))
    period = float(np.median(np.diff(beats)))
    j = np.searchsorted(slots, times)
    lo = slots[np.clip(j - 1, 0, slots.size - 1)]
    hi = slots[np.clip(j, 0, slots.size - 1)]
    dist = np.minimum(np.abs(times - lo), np.abs(times - hi))
    keep = dist <= tolerance * period
    return times[keep], accents[keep]


def _rolling_accents(raw: np.ndarray, half_window: int = 8) -> np.ndarray:
    """Per-beat accents normalised by a rolling p90 of the surrounding beats.

    Normalising by the single loudest onset of the whole track (the old way)
    let one drop impact compress every ordinary kick to ~0.3 — the show's
    pulses all came out half-strength. A rolling reference keeps "strong"
    meaning strong *relative to the passage*, so the verse still has visible
    hierarchy and the chorus still has headroom.
    """
    n = raw.size
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        seg = raw[max(0, i - half_window) : min(n, i + half_window + 1)]
        ref = float(np.percentile(seg, 90))
        out[i] = min(1.0, float(raw[i]) / ref) if ref > 1e-9 else 0.0
    return out


def _find_downbeat(bass_env: np.ndarray, beat_frames: np.ndarray) -> int:
    """Index (0..3) into the beat list where bars start (strongest bass fold)."""
    if beat_frames.size < 8:
        return 0
    accents = bass_env[np.minimum(beat_frames, bass_env.size - 1)]
    sums = [float(accents[k::4].sum()) for k in range(4)]
    return int(np.argmax(sums))


def _segment_sections(
    bands: np.ndarray, rms: np.ndarray, duration: float
) -> list[Section]:
    """Novelty-based sectioning + per-section relative energy."""
    block = max(1, int(round(_BLOCK_S / _FRAME_PERIOD)))
    n_blocks = bands.shape[0] // block
    if n_blocks < 2 * _KERNEL_BLOCKS:
        # Track too short to segment meaningfully: one section, full energy.
        return [Section(0.0, duration, 1.0)]
    feat = bands[: n_blocks * block].reshape(n_blocks, block, -1).mean(axis=1)
    feat = np.log1p(10.0 * feat)
    norms = np.linalg.norm(feat, axis=1, keepdims=True)
    feat = feat / np.maximum(norms, 1e-9)
    ssm = feat @ feat.T  # cosine self-similarity

    # Checkerboard kernel: +1 on same-side quadrants, −1 across the boundary,
    # tapered so the novelty peaks exactly on section changes.
    k = _KERNEL_BLOCKS
    quad = np.ones((2 * k, 2 * k))
    quad[:k, k:] = -1.0
    quad[k:, :k] = -1.0
    kernel = np.outer(np.hanning(2 * k), np.hanning(2 * k)) * quad

    novelty = np.zeros(n_blocks)
    for i in range(k, n_blocks - k):
        novelty[i] = float(np.sum(ssm[i - k : i + k, i - k : i + k] * kernel))

    # Peak-pick boundaries: local maxima well above the robust noise level
    # (median + 5*MAD ~ 3.4 sigma). A plain mean+std threshold fragments
    # uniform tracks, because with flat novelty *everything* sits near it.
    core = novelty[k : n_blocks - k]
    med = float(np.median(core))
    mad = float(np.median(np.abs(core - med)))
    thresh = med + 5.0 * max(mad, 1e-6)
    min_gap = max(1, int(round(_MIN_SECTION_S / _BLOCK_S)))
    bounds: list[int] = []
    for i in range(1, n_blocks - 1):
        if novelty[i] >= thresh and novelty[i] >= novelty[i - 1] and novelty[i] >= novelty[i + 1]:
            if not bounds or i - bounds[-1] >= min_gap:
                bounds.append(i)

    edges = [0.0] + [b * _BLOCK_S for b in bounds] + [duration]
    rms_blocks = rms[: n_blocks * block].reshape(n_blocks, block).mean(axis=1)
    peak = float(np.percentile(rms_blocks, 95)) or 1.0
    sections: list[Section] = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b - a < 1.0:
            continue
        i0 = min(n_blocks - 1, int(a / _BLOCK_S))
        i1 = max(i0 + 1, min(n_blocks, int(b / _BLOCK_S)))
        level = float(np.clip(rms_blocks[i0:i1].mean() / max(peak, 1e-9), 0.0, 1.0))
        sections.append(Section(float(a), float(b), level))
    return sections or [Section(0.0, duration, 1.0)]


def analyze_pcm(pcm: np.ndarray, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> TrackMap | None:
    """Build a TrackMap from decoded mono float32 PCM (synchronous, CPU-bound)."""
    ex = EnvelopeExtractor(sample_rate)
    # Feed in slices to bound the vectorised batch sizes.
    step = sample_rate * 30
    for i in range(0, pcm.size, step):
        ex.push(pcm[i : i + step])
    return _finish_analysis(ex)


def _finish_analysis(ex: EnvelopeExtractor) -> TrackMap | None:
    env = np.asarray(ex.env, dtype=np.float64)
    if env.size < 200:  # < ~4 s of audio
        return None
    bass_env = np.asarray(ex.bass_env, dtype=np.float64)
    rms = np.asarray(ex.rms, dtype=np.float64)
    bands = np.asarray(ex.bands, dtype=np.float32)
    duration = env.size * _FRAME_PERIOD

    bpm, confidence = _estimate_tempo(env)
    beat_frames = _track_beats(env, bpm)
    if beat_frames.size < 4:
        return None
    beats = beat_frames.astype(np.float64) * _FRAME_PERIOD
    # Steady-groove rescue: the normalised autocorrelation dilutes on long
    # quiet passages / big dynamics, failing perfectly steady grooves (a real
    # funk track measured 0.29 against the 0.30 usability gate with only a 3%
    # beat-interval wobble over 4 minutes). The tracked grid itself is better
    # evidence: when the beats land on strong onset-envelope peaks (contrast
    # well above the ~2.9 a noise floor produces) and the grid is tight and
    # covers the track, lift the confidence — capped, so a rescued map never
    # outranks a true autocorrelation lock.
    confidence = _rescue_confidence(confidence, env, beat_frames, beats, duration)
    idx = np.minimum(beat_frames, env.size - 1)
    accents_raw = env[idx]
    # Bass-weight each beat's accent (same formula as the live tracker:
    # flux * (0.5 + 0.5*bass)): rhythmic "impact" is perceived almost entirely
    # in the low end, so a treble stab shouldn't read as a big beat.
    bass_at = bass_env[np.minimum(idx, bass_env.size - 1)]
    bass_ref = float(np.percentile(bass_at, 95)) if bass_at.size else 0.0
    if bass_ref > 1e-9:
        accents_raw = accents_raw * (0.5 + 0.5 * np.clip(bass_at / bass_ref, 0.0, 1.0))
    accents = _rolling_accents(accents_raw)
    downbeat = _find_downbeat(bass_env, beat_frames)
    # Refine the bpm from the actual tracked beats (more honest than the lag).
    intervals = np.diff(beats)
    if intervals.size:
        bpm = float(60.0 / np.median(intervals))
    sections = _segment_sections(bands, rms, duration)
    _fill_section_palettes(sections, ex)
    mid_env = np.asarray(ex.mid_env, dtype=np.float64)
    mid_dom = np.asarray(ex.mid_dom, dtype=bool)
    mid_beats, mid_accents = _pick_onsets(mid_env, allowed=mid_dom)
    mid_energy = bands[:, ex.n_bass : ex.n_mid].sum(axis=1).astype(np.float64)
    mid_beats, mid_accents = _percussive_filter(mid_beats, mid_accents, mid_energy)
    mid_beats, mid_accents = _quantize_to_eighths(mid_beats, mid_accents, beats)
    return TrackMap(
        duration=duration,
        bpm=bpm,
        confidence=confidence,
        beats=beats,
        accents=accents,
        downbeat=downbeat,
        sections=sections,
        features=_build_features(ex, env, bass_env, mid_env, rms),
        mid_beats=mid_beats,
        mid_accents=mid_accents,
    )


def _fill_section_palettes(sections: list[Section], ex: EnvelopeExtractor) -> None:
    """Derive each section's harmonic palette from its mean chroma + centroid."""
    chroma = np.asarray(ex.chroma, dtype=np.float64)
    centroid = np.asarray(ex.centroid, dtype=np.float64)
    n = chroma.shape[0]
    if n == 0:
        return
    for s in sections:
        i0 = max(0, int(s.start / _FRAME_PERIOD))
        i1 = min(n, max(i0 + 1, int(s.end / _FRAME_PERIOD)))
        seg = chroma[i0:i1]
        if seg.size == 0:
            continue
        cval = float(centroid[i0:i1].mean()) if centroid.size else 0.5
        pal = palette_from_chroma(seg.mean(axis=0), cval)
        if pal is not None:
            s.palette = pal.colors


def _build_melbank(mel: np.ndarray) -> np.ndarray:
    """Per-bin gain-normalise + asymmetric-smooth the offline melbank.

    Mirrors the live analyzer's per-bin AGC (here a global p95 reference, since
    the whole track is known) and its :class:`ExpFilter` smoothing, so scheduled
    playback feeds the engine the same continuous reactive texture as the tap.
    """
    if mel.ndim != 2 or mel.shape[0] == 0:
        return np.zeros((0, 0), dtype=np.float32)
    out = np.empty_like(mel, dtype=np.float32)
    floor = 0.02 * max(float(np.percentile(mel, 95)), 1e-6)
    for c in range(mel.shape[1]):
        ref = max(float(np.percentile(mel[:, c], 95)), floor)
        out[:, c] = np.clip(mel[:, c] / ref, 0.0, 1.0)
    filt = ExpFilter(
        np.zeros(mel.shape[1], dtype=np.float32), alpha_rise=0.85, alpha_decay=0.20
    )
    for i in range(out.shape[0]):
        out[i] = filt.update(out[i])
    return out


def _norm_p(a: np.ndarray, pct: float, floor: float) -> np.ndarray:
    """Normalise so the ``pct`` percentile maps to 1.0 (clipped 0..1).

    ``floor`` is an absolute minimum reference so a band that is genuinely
    silent throughout the track doesn't get its noise amplified to full scale
    (the offline counterpart of the live analyzer's noise gate).
    """
    ref = float(np.percentile(a, pct))
    return np.clip(a / max(ref, floor), 0.0, 1.0).astype(np.float32)


def _build_features(
    ex: EnvelopeExtractor,
    env: np.ndarray,
    bass_env: np.ndarray,
    mid_env: np.ndarray,
    rms: np.ndarray,
) -> TrackFeatures:
    """Globally-normalised playback features (offline equivalent of the AGC)."""
    named = np.asarray(ex.named_bands, dtype=np.float32)
    bands = np.empty_like(named)
    for c in range(named.shape[1]):
        bands[:, c] = _norm_p(named[:, c], 95.0, floor=0.5)
    return TrackFeatures(
        bands=bands,
        energy=_norm_p(rms, 95.0, floor=0.01),
        # Onset envelopes are sparse spikes: anchor on a high percentile so a
        # typical beat lands near 1.0 like the live flux AGC.
        flux=_norm_p(env, 99.0, floor=2.5),
        bass_flux=_norm_p(bass_env, 99.0, floor=2.5),
        mid_flux=_norm_p(mid_env, 99.0, floor=2.5),
        centroid=np.asarray(ex.centroid, dtype=np.float32),
        melbank=_build_melbank(np.asarray(ex.melbank, dtype=np.float32)),
    )


def _decode_and_analyze(ffmpeg_bin: str, url: str, max_seconds: float) -> MapResult:
    """Blocking: stream-decode ``url`` with ffmpeg and analyse it on the fly.

    Captures ffmpeg's stderr so a server error (auth, 404, transcode failure) is
    surfaced instead of vanishing, and reports how much audio actually decoded so
    the caller can tell a transient fetch failure from an unanalysable track.
    """
    args = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error",
        "-i", url,
        "-t", f"{max_seconds:.0f}",
        "-vn", "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE),
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as err:
        _LOGGER.debug("Track-map decode failed to start: %s", err)
        return MapResult(None, decoded=False, error=str(err))
    ex = EnvelopeExtractor()
    n_bytes = 0
    try:
        assert proc.stdout is not None
        while True:
            raw = proc.stdout.read(1 << 18)  # 256 KiB ~ 3 s of audio
            if not raw:
                break
            n_bytes += len(raw)
            ex.push(np.frombuffer(raw, dtype="<f4"))
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except OSError:
            pass
        # ffmpeg with -loglevel error emits only a few lines of stderr, so reading
        # it after stdout drains can't deadlock; it tells us why a fetch failed.
        err_txt = ""
        if proc.stderr is not None:
            try:
                err_txt = proc.stderr.read().decode(errors="replace").strip()
                proc.stderr.close()
            except OSError:
                pass
        proc.kill()
        proc.wait()
    decoded = (n_bytes / 4 / ANALYSIS_SAMPLE_RATE) >= _MIN_DECODE_S  # 4 bytes/f32
    tm = _finish_analysis(ex)
    error = "" if (tm is not None or decoded) else (
        err_txt.splitlines()[-1] if err_txt else "no audio decoded"
    )
    return MapResult(track_map=tm, decoded=decoded, error=error)


async def build_track_map(
    ffmpeg_bin: str, url: str, max_seconds: float = _MAX_TRACK_S
) -> MapResult:
    """Decode + analyse a track in an executor, bounded by a timeout."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _decode_and_analyze, ffmpeg_bin, url, max_seconds),
            timeout=_DECODE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _LOGGER.info("Track-map analysis timed out for %s", url)
        return MapResult(None, decoded=False, error="analysis timed out")


# One ffmpeg decode at a time across the WHOLE integration (every area's mapper
# plus the library pre-warm), so background analysis can never run two decodes
# at once and starve the render loops. Created lazily and rebound if the running
# loop changes (so unit tests using fresh ``asyncio.run`` loops still work).
_GLOBAL_LOCK: "asyncio.Lock | None" = None
_GLOBAL_LOCK_LOOP = None


def _global_analysis_lock() -> "asyncio.Lock":
    global _GLOBAL_LOCK, _GLOBAL_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _GLOBAL_LOCK is None or _GLOBAL_LOCK_LOOP is not loop:
        _GLOBAL_LOCK = asyncio.Lock()
        _GLOBAL_LOCK_LOOP = loop
    return _GLOBAL_LOCK


@dataclass(slots=True)
class _Failure:
    """Failure bookkeeping for one track, driving the retry/give-up decision."""

    attempts: int = 0
    retry_at: float = 0.0
    permanent: bool = False


class TrackMapper:
    """Background per-track analysis with a small in-memory cache.

    HA-free; the caller supplies a task spawner (e.g. Home Assistant's
    ``async_create_background_task``) so analysis never blocks the render loop.

    A *transient* failure (decode/network/timeout — e.g. a busy Navidrome) is
    retried with backoff rather than permanently disabling the track; only a
    decoded-but-unusable result (which won't improve on re-analysis) or an
    exhausted retry budget marks a track failed for the session.
    """

    def __init__(
        self,
        ffmpeg_bin: str,
        spawner: Callable[[Coroutine, str], asyncio.Task] | None = None,
        max_cache: int = 12,
        cache_dir: str | Path | None = None,
        max_disk: int = 512,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._spawn = spawner or (lambda coro, name: asyncio.create_task(coro, name=name))
        self._cache: OrderedDict[str, TrackMap] = OrderedDict()  # usable maps only
        self._failures: OrderedDict[str, _Failure] = OrderedDict()
        self._ensuring: set[str] = set()  # track_ids with a disk probe in flight
        self._max_cache = max_cache
        self._task: asyncio.Task | None = None
        self._inflight: str | None = None
        self._stop_prewarm = False
        # Persistent on-disk cache: a played/prefetched/pre-warmed track's map is
        # written here and reloaded instantly on the next play, so the offline
        # route has no per-track analysis delay once a track has been seen.
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._max_disk = max_disk
        if self._cache_dir is not None:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                _LOGGER.warning("Track-map cache dir unavailable: %s", self._cache_dir)
                self._cache_dir = None

    def _disk_path(self, track_id: str) -> Path | None:
        if self._cache_dir is None:
            return None
        name = hashlib.sha1(track_id.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{name}.npz"

    def _load_disk(self, track_id: str) -> TrackMap | None:
        p = self._disk_path(track_id)
        if p is None or not p.exists():
            return None
        tm = TrackMap.load(p)
        return tm if tm is not None and tm.usable else None

    def _save_disk(self, track_id: str, tm: TrackMap) -> None:
        p = self._disk_path(track_id)
        if p is None:
            return
        try:
            tm.save(p)
        except OSError:
            _LOGGER.debug("Track-map disk write failed for %s", track_id, exc_info=True)
            return
        self._prune_disk()

    def _prune_disk(self) -> None:
        """Keep the disk cache under ``max_disk`` files (evict oldest by mtime)."""
        if self._cache_dir is None:
            return
        try:
            files = sorted(self._cache_dir.glob("*.npz"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return
        for f in files[:-self._max_disk] if len(files) > self._max_disk else []:
            try:
                f.unlink()
            except OSError:
                pass

    def has_disk(self, track_id: str | None) -> bool:
        """True if a usable map for ``track_id`` is already on disk (pre-warm check)."""
        p = self._disk_path(track_id) if track_id else None
        return bool(p and p.exists())

    def get(self, track_id: str | None) -> TrackMap | None:
        if not track_id:
            return None
        tm = self._cache.get(track_id)
        return tm if tm is not None and tm.usable else None

    def failed(self, track_id: str | None) -> bool:
        """True only when ``track_id`` has *permanently* failed analysis.

        While a transient failure is still within its retry budget this returns
        False, so the caller keeps the track-map source (placeholder frames)
        instead of dropping to the metadata animation.
        """
        f = self._failures.get(track_id) if track_id else None
        return bool(f and f.permanent)

    def ensure(self, track_id: str | None, url: str | None) -> None:
        """Make a map available for ``track_id``: load from disk, else analyse.

        Called ~1 Hz from the event loop, so the disk read (numpy decompressing
        a cache file) runs in an executor — it must never block the loop. On a
        disk hit the map shows up in :meth:`get` on a later poll (typically the
        very next one); callers already poll, so nothing else changes.
        """
        if not track_id or track_id in self._cache:
            return
        if track_id in self._ensuring:
            return  # disk probe / analysis hand-off already underway
        self._ensuring.add(track_id)
        self._spawn(
            self._ensure_task(track_id, url),
            f"hue_music_sync_mapload_{track_id[:24]}",
        )

    async def ensure_ready(self, track_id: str | None, url: str | None) -> "TrackMap | None":
        """Awaitable :meth:`ensure`: probe the disk cache *now* and return the map.

        For callers that need the answer in-line (``TrackMapSource.open`` deciding
        between track-map and metadata playback for a cached single track). Returns
        the usable map when one is in memory or on disk; otherwise kicks background
        analysis exactly like :meth:`ensure` and returns None.
        """
        if not track_id:
            return None
        tm = self.get(track_id)
        if tm is not None:
            return tm
        if track_id not in self._ensuring:
            self._ensuring.add(track_id)
            try:
                await self._ensure_async(track_id, url)
            finally:
                self._ensuring.discard(track_id)
        return self.get(track_id)

    async def _ensure_task(self, track_id: str, url: str | None) -> None:
        try:
            await self._ensure_async(track_id, url)
        finally:
            self._ensuring.discard(track_id)

    async def _ensure_async(self, track_id: str, url: str | None) -> None:
        disk = await asyncio.get_running_loop().run_in_executor(
            None, self._load_disk, track_id
        )
        if disk is not None:
            self._cache[track_id] = disk
            self._cache.move_to_end(track_id)
            while len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
            self._failures.pop(track_id, None)
            return
        if not url:
            return
        f = self._failures.get(track_id)
        if f and (f.permanent or time.monotonic() < f.retry_at):
            return  # permanently failed, or waiting out the retry backoff
        if self._task is not None and not self._task.done():
            return  # one analysis at a time; retried on the next track poll
        self._inflight = track_id
        self._task = self._spawn(
            self._analyze(track_id, url), f"hue_music_sync_trackmap_{track_id[:24]}"
        )

    def _analysis_lock(self) -> "asyncio.Lock":
        return _global_analysis_lock()

    async def _analyze(self, track_id: str, url: str) -> None:
        try:
            async with self._analysis_lock():  # yields to / blocks the pre-warm
                result = await build_track_map(self._ffmpeg, url)
        except Exception:  # noqa: BLE001 - analysis must never break sync
            _LOGGER.debug("Track-map analysis crashed for %s", url, exc_info=True)
            result = MapResult(None, decoded=False, error="analysis crashed")
        finally:
            self._inflight = None
        self._record_result(track_id, url, result)
        # Persist a good map so the next play is instant (write off the loop —
        # a ~0.3 MB compressed save shouldn't stall the render frames).
        tm = result.track_map
        if tm is not None and tm.usable and self._cache_dir is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._save_disk, track_id, tm
                )
            except Exception:  # noqa: BLE001 - caching must never break sync
                _LOGGER.debug("Track-map disk save failed for %s", track_id, exc_info=True)

    def _record_result(self, track_id: str, url: str, result: MapResult) -> None:
        tm = result.track_map
        if tm is not None and tm.usable:
            self._failures.pop(track_id, None)
            self._cache[track_id] = tm
            while len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
            _LOGGER.info(
                "Track map ready: %.0f s, %.1f BPM (confidence %.2f), "
                "%d beats, %d sections",
                tm.duration, tm.bpm, tm.confidence, tm.beats.size, len(tm.sections),
            )
            return

        f = self._failures.get(track_id) or _Failure()
        f.attempts += 1
        if result.decoded:
            # Audio decoded but no usable beat map — re-analysing won't help.
            # WARNING: this permanently disables track-map sync for the track,
            # which reads as "this song doesn't react" — it must be visible.
            f.permanent = True
            _LOGGER.warning(
                "Track-map analysis produced no usable map for %s "
                "(decoded but unanalysable); using live/fallback sync", url
            )
        elif f.attempts >= _MAX_ANALYSIS_ATTEMPTS:
            f.permanent = True
            _LOGGER.warning(
                "Track-map analysis failed %d times for %s (%s); giving up for "
                "this track", f.attempts, url, result.error or "unknown error"
            )
        else:
            delay = min(_RETRY_MAX_S, _RETRY_BASE_S * (2 ** (f.attempts - 1)))
            f.retry_at = time.monotonic() + delay
            _LOGGER.info(
                "Track-map analysis failed for %s (%s); retry %d/%d in %.0fs",
                url, result.error or "unknown error",
                f.attempts, _MAX_ANALYSIS_ATTEMPTS, delay,
            )
        self._failures[track_id] = f
        self._failures.move_to_end(track_id)
        while len(self._failures) > self._max_cache * 2:
            self._failures.popitem(last=False)

    async def prewarm(
        self,
        items,
        *,
        delay_s: float = 1.0,
        progress=None,
    ) -> tuple[int, int, list[tuple[str, str]]]:
        """Analyse + cache every uncached ``(track_id, url)`` one at a time.

        A gentle, resumable background sweep of a music library so a track plays
        instantly (offline track-map) the first time too. Already-cached tracks
        (in memory or on disk) and permanently-failed ones are skipped, so a
        re-run continues where it left off. The shared analysis lock means a
        live playback analysis takes priority — the pre-warm waits between
        tracks and never decodes two streams at once. Returns
        ``(analysed, considered, failures)`` where ``failures`` is up to 20
        ``(url, error)`` samples — a systematically failing library (bad login,
        wrong URL scheme) must be visible, not silent.
        """
        self._stop_prewarm = False
        analysed = considered = failed = 0
        failures: list[tuple[str, str]] = []
        for track_id, url in items:
            if self._stop_prewarm:
                break
            considered += 1
            try:
                if not track_id or not url:
                    continue
                if track_id in self._cache or self.has_disk(track_id) or self.failed(track_id):
                    continue
                try:
                    async with self._analysis_lock():
                        result = await build_track_map(self._ffmpeg, url)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad track must not stop the sweep
                    _LOGGER.debug("Pre-warm analysis crashed for %s", url, exc_info=True)
                    result = MapResult(None, decoded=False, error="prewarm crashed")
                self._record_result(track_id, url, result)
                tm = result.track_map
                if tm is not None and tm.usable:
                    if self._cache_dir is not None:
                        try:
                            await asyncio.get_running_loop().run_in_executor(
                                None, self._save_disk, track_id, tm
                            )
                        except Exception:  # noqa: BLE001
                            _LOGGER.debug(
                                "Pre-warm disk save failed for %s", track_id, exc_info=True
                            )
                    analysed += 1
                else:
                    failed += 1
                    err = result.error or (
                        "decoded but unanalysable" if result.decoded else "unknown error"
                    )
                    if len(failures) < 20:
                        failures.append((url, err))
                    if failed <= 10:  # first few at WARNING; a broken library must show
                        _LOGGER.warning(
                            "Library pre-warm: analysis failed for %s (%s)", url, err
                        )
                    elif failed == 11:
                        _LOGGER.warning(
                            "Library pre-warm: more analysis failures; further ones "
                            "logged at debug level only"
                        )
                await asyncio.sleep(delay_s)  # be gentle on CPU + the music library
            finally:
                # Every item counts as progress (cached/skipped ones too), so a
                # progress surface moves smoothly instead of stalling on a
                # mostly-cached library.
                if progress is not None:
                    progress(analysed, considered)
        return analysed, considered, failures

    def stop_prewarm(self) -> None:
        """Ask an in-flight :meth:`prewarm` sweep to stop after the current track."""
        self._stop_prewarm = True

    async def close(self) -> None:
        self._stop_prewarm = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
