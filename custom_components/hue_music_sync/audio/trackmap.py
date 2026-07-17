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
    FFMPEG_PROTOCOL_ARGS,
    MELBANK_BINS,
)
from ..util import redact_url
from .analyzer import (
    LONG_FLUX_FLOOR_FRAC,
    AnalysisFrame,
    band_means,
    chroma_projection,
    log_spectrum,
    make_melbank,
    make_onset_filterbank,
    max_filter_freq,
)
from .filters import ExpFilter
from .tempo import BeatGrid
from .track_index import TrackIndex, track_key

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

# Use a track map's BEAT GRID only when the analysis was confident enough to
# trust. Below this the map is still served (features/sections/colours — the
# "ambient" tier), it just doesn't claim scheduled beats.
MIN_MAP_CONFIDENCE = 0.30

# Features are worth serving on their own once this much audio was analysed:
# anything shorter is almost certainly a truncated fetch, and a jingle that
# short loses little by falling back to live/metadata sync.
_MIN_FEATURES_S = 30.0

# 95th-percentile RMS below this is digital silence — a "track" of nothing must
# stay unanalysable rather than becoming an all-dark ambient map.
_MIN_AUDIO_RMS = 1e-4

# On-disk cache format version: bump when the serialised layout changes so stale
# files are ignored rather than mis-read. Stored in each .npz's ``meta`` row.
# v2: per-frame onset width (``f_width``) + long-horizon floor on mid picks.
# v3: detected-onset arrays for gridless (ambient) maps + the ``diag`` row.
# v4: per-frame stereo pan (``f_pan``, int8-quantised melbank balance).
# Older files load fine (their layout is a strict subset; pan is simply absent
# and the engine renders exactly as before pan existed), so a format bump does
# NOT silently un-warm the pre-analysed library — no rescan, ever. Any FUTURE
# incompatible bump must either extend _COMPAT_FORMATS or add a one-time format
# scan to the pre-warm — has_disk() deliberately never opens files, so it
# cannot tell a stale format from a fresh one.
_CACHE_FORMAT = 4
_COMPAT_FORMATS = frozenset({2, 3, 4})

# Offline mid picks narrower than this across the SuperFlux filterbank are
# dropped as vocal/tonal (see AnalysisFrame.onset_width). Deliberately at the
# LOOSEST mode's gate (Extreme's width_min): the mode is unknown at analysis
# time, so anything stricter happens in the engine at render time.
_MID_WIDTH_FLOOR = 0.05

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
class MapDiagnostics:
    """Structured account of one analysis: what was measured and why it landed
    in its tier. This is what the ``analyze_track`` service and the pre-warm
    failure report surface to the user, so a song that "doesn't react" has an
    inspectable reason instead of a vanished log line.
    """

    analysed_s: float = 0.0
    bpm: float = 0.0
    confidence: float = 0.0
    autocorr_conf: float = 0.0  # raw global-autocorrelation confidence
    contrast: float = 0.0  # beats-on-peaks envelope contrast (noise ~2.9)
    mad_local: float = 0.0  # median |interval/local_period - 1|
    coverage: float = 0.0  # (beats[-1]-beats[0]) / duration
    tempo_stability: float = 0.0  # fraction of stable tempo-path transitions
    beats: int = 0
    sections: int = 0
    tier: str = "unusable"  # "full" | "ambient" | "unusable"
    reason: str = ""  # human sentence when tier != "full"

    def as_array(self) -> np.ndarray:
        """The numeric core, persisted in the .npz ``diag`` row."""
        return np.array(
            [self.autocorr_conf, self.contrast, self.mad_local,
             self.coverage, self.tempo_stability, self.analysed_s],
            dtype=np.float64,
        )


@dataclass(slots=True)
class MapResult:
    """Outcome of one analysis attempt.

    ``decoded`` distinguishes "we got the audio but couldn't build a usable map"
    from "we couldn't get the audio" (transient — retry), so a slow/busy
    Navidrome doesn't permanently disable a track. ``complete`` means ffmpeg
    reached the end of the stream cleanly — only then is an unusable result
    treated as permanent (a partial decode may analyse fine next time).
    ``error`` carries the ffmpeg/analysis diagnostic for the log.
    """

    track_map: "TrackMap | None"
    decoded: bool
    error: str = ""
    complete: bool = False
    diagnostics: MapDiagnostics | None = None


@dataclass(slots=True)
class PrewarmResult:
    """Outcome of one library pre-warm sweep."""

    analysed: int = 0  # full-tier maps newly written
    ambient: int = 0  # features-only (ambient) maps newly written
    considered: int = 0
    failed: int = 0
    # (label, url, reason) samples, capped at 20; the full list lives in the
    # shared TrackIndex / analysis report.
    failures: list[tuple[str, str, str]] = field(default_factory=list)


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
    # Per-frame onset width (broadbandness of the SuperFlux, 0..1 — see
    # AnalysisFrame.onset_width), already 0..1 so not normalised. Empty on
    # older analyses (frame_at then reports the neutral 1.0).
    width: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    # Per-frame stereo pan (n_frames, MELBANK_BINS), int8-quantised
    # round(pan*127): -127 hard left .. +127 hard right. Empty on pre-v4
    # analyses and mono decodes (frame_at then reports no pan — mono render).
    # Compresses to near-nothing in the .npz for centred masters.
    pan: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), dtype=np.int8))


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
    # Detected (peak-picked) onsets for gridless playback: honest per-event
    # detections, NOT the rejected beat grid. An ambient-tier map replays these
    # so the engine's unlocked flash path fires and the causal tempo tracker
    # gets phase anchors — the room stays alive without claiming a schedule.
    onsets: np.ndarray = field(default_factory=lambda: np.zeros(0))
    onset_accents: np.ndarray = field(default_factory=lambda: np.zeros(0))
    # Numeric diagnostics core (MapDiagnostics.as_array layout); zeros on maps
    # loaded from pre-v3 cache files.
    diag: np.ndarray = field(default_factory=lambda: np.zeros(6))

    @property
    def grid_usable(self) -> bool:
        """The beat grid is trustworthy enough to schedule the show against."""
        return self.confidence >= MIN_MAP_CONFIDENCE and self.beats.size >= 8

    @property
    def features_usable(self) -> bool:
        """Enough per-frame features to drive the continuous show on their own."""
        return self.features is not None and self.duration >= _MIN_FEATURES_S

    @property
    def servable(self) -> bool:
        """Worth caching + playing back at all (full tier or ambient tier)."""
        return self.grid_usable or self.features_usable

    # Historical alias (tests/scripts read map.usable for the grid gate).
    @property
    def usable(self) -> bool:
        return self.grid_usable

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
            "onsets": self.onsets.astype(np.float64),
            "onset_accents": self.onset_accents.astype(np.float32),
            "diag": np.asarray(self.diag, dtype=np.float64),
        }
        if f is not None:
            data.update({
                "f_bands": f.bands, "f_energy": f.energy, "f_flux": f.flux,
                "f_bass_flux": f.bass_flux, "f_mid_flux": f.mid_flux,
                "f_centroid": f.centroid, "f_melbank": f.melbank,
                "f_width": f.width, "f_pan": f.pan,
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
                if int(round(float(meta[4]))) not in _COMPAT_FORMATS:
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
                        centroid=z["f_centroid"], melbank=z["f_melbank"],
                        width=z["f_width"],
                        # Pre-v4 files have no pan row: mono render, as before.
                        pan=(
                            z["f_pan"] if "f_pan" in z
                            else np.zeros((0, 0), dtype=np.int8)
                        ))
                return cls(
                    duration=float(meta[0]), bpm=float(meta[1]),
                    confidence=float(meta[2]), beats=z["beats"],
                    accents=z["accents"], downbeat=int(round(float(meta[3]))),
                    sections=sections, features=features,
                    mid_beats=z["mid_beats"], mid_accents=z["mid_accents"],
                    # v2 files predate these rows; every v2 map was grid-usable
                    # so the missing onsets are never queried.
                    onsets=z["onsets"] if "onsets" in z else np.zeros(0),
                    onset_accents=(
                        z["onset_accents"] if "onset_accents" in z else np.zeros(0)
                    ),
                    diag=z["diag"] if "diag" in z else np.zeros(6))
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
        # A gridless (ambient) map replays its *detected* onsets instead: honest
        # per-event detections that drive the engine's unlocked flash path and
        # give the causal tempo tracker phase anchors, without pretending the
        # rejected grid is a schedule.
        lo = prev_pos if prev_pos is not None and prev_pos < pos else pos - _FRAME_PERIOD
        if self.grid_usable:
            events, ev_accents = self.beats, self.accents
        else:
            events, ev_accents = self.onsets, self.onset_accents
        j0 = int(np.searchsorted(events, lo, side="right"))
        j1 = int(np.searchsorted(events, pos, side="right"))
        beat = j1 > j0
        # Accent-weighted strength on the live detector's ~1..3 scale, so the
        # per-mode beat_threshold gates behave exactly like the live path.
        strength = (
            1.0 + 2.0 * float(ev_accents[j1 - 1])
            if beat and ev_accents.size >= j1
            else (1.0 if beat else 0.0)
        )
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
            # An ambient map has no trustworthy tempo: report none so nothing
            # downstream schedules against it (the causal tracker finds its own).
            tempo_bpm=self.bpm if self.grid_usable else 0.0,
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
            # Globally p95-normalised energy IS the salience signal (better
            # than the live rolling reference: the whole track was seen at
            # once), so map playback gets the same proportional reactions.
            salience=float(f.energy[i]),
            onset_width=float(f.width[i]) if f.width.shape[0] > i else 1.0,
            pan=(f.pan[i] / 127.0).tolist() if f.pan.shape[0] > i else [],
        )

    def grid_at(self, pos: float, prev_pos: float | None = None) -> BeatGrid | None:
        """The authoritative beat grid at playback position ``pos`` (seconds).

        ``prev_pos`` (the previous query) makes ``predicted_beat`` fire exactly
        once per crossed beat. Returns None outside the analysed span, and for
        an ambient-tier map (no trustworthy grid) — the coordinator then stays
        on its causal tracker, which locks onto the replayed onsets live.
        """
        if not self.grid_usable:
            return None
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
# (The projection itself now lives in analyzer.chroma_projection, shared with
# the live path, which uses a narrower range suited to per-frame estimates.)
_CHROMA_FMIN = 55.0
_CHROMA_FMAX = 5000.0


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
        self._chroma_proj = chroma_projection(freqs, _CHROMA_FMIN, _CHROMA_FMAX)
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
        self.width: list[float] = []  # onset broadbandness per frame (0..1)
        # Per-frame linear-domain mid dominance (same gate as the live
        # detector): without it the offline mid stream fires on every kick's
        # attack splash, and map-driven "guitar" lights pop on the kicks.
        self.mid_dom: list[bool] = []
        self.rms: list[float] = []
        self.bands: list[np.ndarray] = []  # linear filterbank means per frame
        self.named_bands: list[np.ndarray] = []  # the 5 output bands per frame
        self.centroid: list[float] = []
        # Stereo pan inputs (push_stereo only): raw per-frame L/R melbank
        # means, turned into the smoothed quantised pan by _build_features.
        self.mel_l: list[np.ndarray] = []
        self.mel_r: list[np.ndarray] = []
        self._tail_l = np.zeros(0, dtype=np.float32)
        self._tail_r = np.zeros(0, dtype=np.float32)

    def push_stereo(self, left: np.ndarray, right: np.ndarray) -> None:
        """Analyse the mid downmix, plus per-frame L/R melbank means for pan.

        The mid (L+R)/2 is exactly what the mono ``-ac 1`` decode produced, so
        every existing feature and threshold is unchanged; the side channels
        only feed the pan. Framing mirrors :meth:`push` (same window/hop and
        tail handling), so the pan rows stay 1:1 with the feature frames.
        """
        self.push(0.5 * (left + right))
        self._tail_l = self._push_side(left, self._tail_l, self.mel_l)
        self._tail_r = self._push_side(right, self._tail_r, self.mel_r)

    def _push_side(
        self, samples: np.ndarray, tail: np.ndarray, out: list[np.ndarray]
    ) -> np.ndarray:
        buf = np.concatenate([tail, samples]) if tail.size else samples
        if buf.size < self._window:
            return buf
        n_frames = 1 + (buf.size - self._window) // self._hop
        frames = np.lib.stride_tricks.sliding_window_view(buf, self._window)[
            :: self._hop
        ][:n_frames]
        mags = np.abs(np.fft.rfft(frames * self._hann, axis=1)).astype(np.float32)
        out.extend(band_means(mags, self._mel_starts, self._mel_counts))
        return buf[n_frames * self._hop :].copy()

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
        # Onset width (normalised inverse participation ratio of the positive
        # flux — same maths as the live analyzer): drums are broadband, a
        # sung vowel / sustained tone concentrates in a few bands.
        s1 = diff.sum(axis=1)
        s2 = (diff * diff).sum(axis=1)
        width = (s1 * s1) / (diff.shape[1] * np.maximum(s2, 1e-12))
        width[s2 <= 1e-12] = 0.0
        self.width.extend(width.tolist())
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
# ~2.9, real steady grooves 4.2-5.7); credit scales in over the next 1.6.
_CONTRAST_FLOOR = 3.4
_CONTRAST_RANGE = 1.6

# Local-tempo path (tempogram + Viterbi): sees a drifting live drummer as a
# sequence of locally-steady tempi instead of one smeared global peak.
_TG_WIN_S = 10.0  # tempogram analysis window
_TG_HOP_S = 1.0  # tempogram hop
_TG_WHITEN_S = 0.5  # rolling-mean subtraction before autocorrelation
_TEMPO_TRANS_PENALTY = 40.0  # Viterbi log2(lag ratio) transition weight
_PATH_STABLE_TOL = 0.04  # adjacent-window |dlog bpm| counted as stable

# Grid-quality confidence (the old "rescue" promoted to primary evidence).
_GRID_CONF_CAP = 0.85
_TIGHT_MAD_FULL = 0.05  # residual MAD vs the LOCAL grid <= this: full credit
_TIGHT_MAD_ZERO = 0.25  # >= this: no tightness credit


# Autocorrelation upsampling factor: the beat period is fractional in frames
# (140 BPM = 21.43 frames) while spiky onset envelopes make the true peak
# sharper than the frame grid — sampled at integer lags, the true period can
# score LOWER than its double (42.86 ~ 43 aligns; 21.43 splits between 21 and
# 22), a guaranteed octave error. Band-limited upsampling (zero-padded inverse
# FFT) recovers the fractional-lag peaks before any lag is scored.
_TG_UPSAMPLE = 4


def _tempogram(
    env: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Sliding-window autocorrelation tempogram of the onset envelope.

    Returns ``(salience[n_win, n_lag], lags, window_centre_frames,
    lag_refined[n_win, n_lag])`` with the same log-Gaussian tempo prior and
    harmonic-comb support as the global estimator, applied per window — or
    None when the track is too short. Each window is "whitened" (short rolling
    mean subtracted) first, so long quiet passages and slow dynamics can't
    dilute the local peaks the way they dilute the global autocorrelation.
    ``lag_refined`` carries each candidate's fractional-lag peak position from
    the upsampled autocorrelation (see ``_TG_UPSAMPLE``).
    """
    n = env.size
    win = int(round(_TG_WIN_S / _FRAME_PERIOD))
    hop = int(round(_TG_HOP_S / _FRAME_PERIOD))
    min_lag = max(1, int(round(60.0 / _MAX_BPM / _FRAME_PERIOD)))
    max_lag = int(round(60.0 / _MIN_BPM / _FRAME_PERIOD))
    if n < win or max_lag * 3 >= win:
        return None
    k = max(1, int(round(_TG_WHITEN_S / _FRAME_PERIOD)))
    white = env - np.convolve(env, np.ones(k) / k, mode="same")
    n_win = 1 + (n - win) // hop
    frames = np.lib.stride_tricks.sliding_window_view(white, win)[::hop][:n_win]
    frames = frames * np.hanning(win)
    # Batch autocorrelation via one FFT (zero-padded to avoid circular wrap),
    # band-limited-upsampled ``_TG_UPSAMPLE``x so fractional-lag peaks exist.
    u = _TG_UPSAMPLE
    nfft = 1 << int(np.ceil(np.log2(2 * win)))
    spec = np.fft.rfft(frames, nfft, axis=1)
    power = (spec * np.conj(spec)).real
    ac = np.fft.irfft(power, u * nfft, axis=1)[:, : u * win]
    zero = np.maximum(ac[:, :1], 1e-12)
    acn = ac / zero
    lags = np.arange(min_lag, max_lag + 1)

    half = u // 2  # +-half a frame around each integer lag on the fine grid

    def _peak(mult: int) -> tuple[np.ndarray, np.ndarray]:
        """(peak value, fractional lag/mult) around each candidate*mult."""
        centres_f = u * mult * lags
        offs = np.arange(-half * mult, half * mult + 1)
        idx = centres_f[None, :, None] + offs[None, None, :]
        vals = acn[:, np.clip(idx[0], 0, acn.shape[1] - 1)]
        best = np.argmax(vals, axis=2)
        peak = np.take_along_axis(vals, best[:, :, None], axis=2)[:, :, 0]
        frac = (centres_f[None, :] + offs[best]) / (u * mult)
        return peak, frac

    base, lag_refined = _peak(1)
    h2, _ = _peak(2)
    h3, _ = _peak(3)
    # Harmonic-comb support: a true beat period shows autocorrelation energy at
    # its multiples too, which an eighth-note false peak lacks.
    sal = base + 0.5 * h2 + 0.34 * h3
    cand_bpm = 60.0 / (lags * _FRAME_PERIOD)
    prior = np.exp(-0.5 * (np.log(cand_bpm / _PRIOR_CENTER_BPM) / _PRIOR_WIDTH) ** 2)
    sal = np.maximum(sal, 0.0) * prior[None, :]
    centres = (win // 2) + hop * np.arange(n_win)
    return sal, lags, centres, lag_refined


def _tempo_path(
    sal: np.ndarray, lags: np.ndarray, lag_refined: np.ndarray | None = None
) -> tuple[np.ndarray, float]:
    """Viterbi-decode the most likely tempo trajectory through the tempogram.

    Transitions pay ``-_TEMPO_TRANS_PENALTY * log2(lag_i/lag_j)`` so the path
    rides genuine drift and real tempo changes but cannot flap between octave
    candidates window-to-window. Returns ``(lag_per_window_float, stability)``
    where the fractional lags come from the upsampled-autocorrelation peaks
    (``lag_refined``) and stability is the fraction of adjacent transitions
    below ``_PATH_STABLE_TOL``.
    """
    n_win, n_lag = sal.shape
    obs = np.log(sal + 1e-6)
    ll = np.log(lags.astype(np.float64))
    trans = -_TEMPO_TRANS_PENALTY * (ll[:, None] - ll[None, :]) ** 2  # [to, from]
    score = obs[0].copy()
    back = np.zeros((n_win, n_lag), dtype=np.int64)
    for t in range(1, n_win):
        cand = trans + score[None, :]
        back[t] = np.argmax(cand, axis=1)
        score = cand[np.arange(n_lag), back[t]] + obs[t]
    path = np.empty(n_win, dtype=np.int64)
    path[-1] = int(np.argmax(score))
    for t in range(n_win - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    if lag_refined is not None:
        lag_f = lag_refined[np.arange(n_win), path].astype(np.float64)
    else:
        lag_f = lags[path].astype(np.float64)
    dlog = np.abs(np.diff(np.log(lag_f)))
    stability = float(np.mean(dlog < _PATH_STABLE_TOL)) if dlog.size else 1.0
    return lag_f, stability


def _grid_confidence(
    env: np.ndarray,
    beat_frames: np.ndarray,
    beats: np.ndarray,
    period_frames: np.ndarray,
    stability: float,
    duration: float,
    autocorr_conf: float,
) -> tuple[float, float, float, float]:
    """Grid-quality-first confidence: ``(confidence, mad_local, contrast, coverage)``.

    The global autocorrelation dilutes on dynamics and smears on drift, so the
    tracked grid itself is the primary evidence: beats-on-peaks CONTRAST (the
    one thing the DP tracker's regularising bias cannot fake — it is a hard
    multiplier, so noise scores zero), tightness of the intervals against the
    LOCAL tempo path (honest under drift, unlike a global-period MAD), coverage
    of the track, and the tempo path's own stability. The best of this and the
    raw autocorrelation confidence wins; a perfect global lock still tops the
    ``_GRID_CONF_CAP``.
    """
    if beats.size < 16 or duration < 10.0:
        return autocorr_conf, 0.0, 0.0, 0.0
    intervals = np.diff(beats)
    med = float(np.median(intervals))
    if med <= 0 or not (60.0 / _MAX_BPM <= med <= 60.0 / _MIN_BPM):
        return autocorr_conf, 0.0, 0.0, 0.0
    mean = float(env.mean())
    if mean <= 1e-9:
        return autocorr_conf, 0.0, 0.0, 0.0
    mid_idx = np.clip(
        (beat_frames[:-1] + beat_frames[1:]) // 2, 0, period_frames.size - 1
    )
    local_p = period_frames[mid_idx] * _FRAME_PERIOD
    mad_local = float(np.median(np.abs(intervals / np.maximum(local_p, 1e-6) - 1.0)))
    coverage = float((beats[-1] - beats[0]) / max(duration, 1e-6))
    idx = np.minimum(beat_frames, env.size - 1)
    contrast = float(env[idx].mean()) / mean
    contrast_s = float(np.clip((contrast - _CONTRAST_FLOOR) / _CONTRAST_RANGE, 0.0, 1.0))
    tight_s = float(np.clip(
        (_TIGHT_MAD_ZERO - mad_local) / (_TIGHT_MAD_ZERO - _TIGHT_MAD_FULL), 0.0, 1.0
    ))
    cov_s = float(np.clip((coverage - 0.5) / 0.4, 0.0, 1.0))
    grid_conf = min(
        _GRID_CONF_CAP,
        contrast_s * (0.55 + 0.20 * tight_s + 0.15 * cov_s + 0.10 * stability),
    )
    return max(autocorr_conf, grid_conf), mad_local, contrast, coverage


def _track_beats(env: np.ndarray, bpm: float) -> np.ndarray:
    """Ellis DP beat tracker with a constant tempo; returns beat frame indices."""
    if bpm <= 0 or env.size < 16:
        return np.zeros(0, dtype=np.int64)
    period = 60.0 / bpm / _FRAME_PERIOD  # frames per beat
    return _track_beats_local(env, np.full(env.size, period))


def _track_beats_local(env: np.ndarray, period: np.ndarray) -> np.ndarray:
    """Ellis dynamic-programming beat tracker with a per-frame tempo period.

    Maximises sum(onset strength at beats) − tightness·Σ log²(interval/period):
    the globally optimal beat sequence for the tempo estimate. ``period`` is
    frames-per-beat per frame — a constant array reproduces the classic
    tracker, a tempogram-derived path lets the same DP ride a drifting drummer
    or a genuine mid-song tempo change without losing its regularising bias.
    """
    if env.size < 16 or period.size != env.size or float(period.min()) <= 0:
        return np.zeros(0, dtype=np.int64)
    # Normalise the onset envelope to unit std so _TIGHTNESS is scale-free.
    std = env.std()
    local = env / std if std > 1e-9 else env
    n = local.size
    lo = -int(round(2.0 * float(period.max())))
    hi = -max(1, int(round(float(period.min()) / 2.0)))
    prange = np.arange(lo, hi + 1)
    log_neg = np.log(-prange.astype(np.float64))
    log_period = np.log(period)
    cumscore = local.copy()
    backlink = np.full(n, -1, dtype=np.int64)
    first_beat = True
    for i in range(max(1, -lo), n):
        txwt = -_TIGHTNESS * (log_neg - log_period[i]) ** 2
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
    tail_start = max(0, n - int(round(float(period[-1]))))
    last = tail_start + int(np.argmax(cumscore[tail_start:]))
    beats = [last]
    while backlink[beats[-1]] >= 0:
        beats.append(int(backlink[beats[-1]]))
    return np.array(beats[::-1], dtype=np.int64)


def _rolling_max(env: np.ndarray, w: int) -> np.ndarray:
    """Cheap per-frame rolling maximum over roughly ±``w`` frames (block max)."""
    n = env.size
    if n == 0 or w <= 1:
        return env.copy()
    nb = (n + w - 1) // w
    pad = np.pad(env, (0, nb * w - n), constant_values=0.0)
    bm = pad.reshape(nb, w).max(axis=1)
    left = np.concatenate([bm[:1], bm[:-1]])
    right = np.concatenate([bm[1:], bm[-1:]])
    per_block = np.maximum(bm, np.maximum(left, right))
    return np.repeat(per_block, w)[:n]


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
    # Long-horizon floor (offline twin of the live detector's): the ~0.9 s
    # window collapses in quiet passages until faint residue "beats"; pin the
    # threshold to a fraction of the passage-scale (~30 s) envelope peak.
    thr = np.maximum(thr, LONG_FLUX_FLOOR_FRAC * _rolling_max(env, 1500))
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


def _width_filter(
    times: np.ndarray,
    accents: np.ndarray,
    width: np.ndarray,
    floor: float = _MID_WIDTH_FLOOR,
) -> tuple[np.ndarray, np.ndarray]:
    """Drop onsets whose SuperFlux was too narrowband to be percussion.

    The offline twin of the engine's width gate, at the loosest mode's floor
    only (mode strictness is applied at render time). A peak can land a hop
    off its widest frame, so each onset gets the best width of ±1 frame.
    """
    if times.size == 0 or width.size == 0:
        return times, accents
    idx = np.clip(np.round(times / _FRAME_PERIOD).astype(np.int64), 0, width.size - 1)
    lo = np.clip(idx - 1, 0, width.size - 1)
    hi = np.clip(idx + 1, 0, width.size - 1)
    best = np.maximum(width[idx], np.maximum(width[lo], width[hi]))
    keep = best >= floor
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
    return analyze_pcm_diag(pcm, sample_rate)[0]


def analyze_pcm_diag(
    pcm: np.ndarray, sample_rate: int = ANALYSIS_SAMPLE_RATE
) -> tuple[TrackMap | None, MapDiagnostics]:
    """:func:`analyze_pcm` plus the structured :class:`MapDiagnostics`."""
    ex = EnvelopeExtractor(sample_rate)
    # Feed in slices to bound the vectorised batch sizes.
    step = sample_rate * 30
    for i in range(0, pcm.size, step):
        ex.push(pcm[i : i + step])
    return _finish_analysis(ex)


def _finish_analysis(ex: EnvelopeExtractor) -> tuple[TrackMap | None, MapDiagnostics]:
    diag = MapDiagnostics()
    env = np.asarray(ex.env, dtype=np.float64)
    if env.size < 200:  # < ~4 s of audio
        diag.reason = "less than ~4 s of audio analysed"
        return None, diag
    bass_env = np.asarray(ex.bass_env, dtype=np.float64)
    rms = np.asarray(ex.rms, dtype=np.float64)
    bands = np.asarray(ex.bands, dtype=np.float32)
    duration = env.size * _FRAME_PERIOD
    diag.analysed_s = duration
    if float(np.percentile(rms, 95)) < _MIN_AUDIO_RMS:
        diag.reason = "audio is silent"
        return None, diag

    bpm, autocorr_conf = _estimate_tempo(env)
    diag.autocorr_conf = autocorr_conf
    # Local tempo path: on anything longer than two tempogram windows, decode
    # the per-window tempo trajectory so a drifting drummer or a genuine
    # mid-song tempo change reads as locally-steady tempo instead of one
    # smeared global autocorrelation peak. Short tracks keep the scalar path.
    stability = 1.0
    period_frames: np.ndarray | None = None
    if duration >= 2.0 * _TG_WIN_S:
        tg = _tempogram(env)
        if tg is not None:
            sal, lags, centres, lag_refined = tg
            lag_path, stability = _tempo_path(sal, lags, lag_refined)
            period_frames = np.interp(
                np.arange(env.size, dtype=np.float64), centres, lag_path
            )
    if period_frames is None and bpm > 0:
        period_frames = np.full(env.size, 60.0 / bpm / _FRAME_PERIOD)
    diag.tempo_stability = stability
    beat_frames = (
        _track_beats_local(env, period_frames)
        if period_frames is not None
        else np.zeros(0, dtype=np.int64)
    )
    beats = beat_frames.astype(np.float64) * _FRAME_PERIOD

    # Grid-quality-first confidence: the tracked grid's own evidence (contrast,
    # tightness vs the LOCAL tempo, coverage, path stability) is the primary
    # measure; the raw autocorrelation confidence only wins when it is higher
    # (a clean global lock). Noise cannot pass: contrast is a hard multiplier.
    confidence = autocorr_conf
    if beat_frames.size >= 2 and period_frames is not None:
        confidence, diag.mad_local, diag.contrast, diag.coverage = _grid_confidence(
            env, beat_frames, beats, period_frames, stability, duration, autocorr_conf
        )

    accents = np.zeros(0)
    downbeat = 0
    if beat_frames.size >= 4:
        idx = np.minimum(beat_frames, env.size - 1)
        accents_raw = env[idx]
        # Bass-weight each beat's accent (same formula as the live tracker:
        # flux * (0.5 + 0.5*bass)): rhythmic "impact" is perceived almost
        # entirely in the low end, so a treble stab shouldn't read as a big beat.
        bass_at = bass_env[np.minimum(idx, bass_env.size - 1)]
        bass_ref = float(np.percentile(bass_at, 95)) if bass_at.size else 0.0
        if bass_ref > 1e-9:
            accents_raw = accents_raw * (
                0.5 + 0.5 * np.clip(bass_at / bass_ref, 0.0, 1.0)
            )
        accents = _rolling_accents(accents_raw)
        downbeat = _find_downbeat(bass_env, beat_frames)
        # Refine the bpm from the tracked beats (more honest than the lag).
        intervals = np.diff(beats)
        if intervals.size:
            bpm = float(60.0 / np.median(intervals))
    else:
        beats = np.zeros(0)
        beat_frames = np.zeros(0, dtype=np.int64)

    grid_ok = confidence >= MIN_MAP_CONFIDENCE and beats.size >= 8
    sections = _segment_sections(bands, rms, duration)
    _fill_section_palettes(sections, ex)
    mid_env = np.asarray(ex.mid_env, dtype=np.float64)
    mid_dom = np.asarray(ex.mid_dom, dtype=bool)
    mid_beats, mid_accents = _pick_onsets(mid_env, allowed=mid_dom)
    mid_energy = bands[:, ex.n_bass : ex.n_mid].sum(axis=1).astype(np.float64)
    mid_beats, mid_accents = _percussive_filter(mid_beats, mid_accents, mid_energy)
    if grid_ok:
        # Quantising to a rejected grid would be worse than no quantising.
        mid_beats, mid_accents = _quantize_to_eighths(mid_beats, mid_accents, beats)
    mid_beats, mid_accents = _width_filter(
        mid_beats, mid_accents, np.asarray(ex.width, dtype=np.float64)
    )
    # Gridless (ambient) playback events: honest detected onsets on the full
    # envelope — the engine's unlocked flash path and the causal tempo tracker
    # get real per-event evidence without a fake schedule.
    onsets = np.zeros(0)
    onset_accents = np.zeros(0)
    if not grid_ok:
        onsets, onset_accents = _pick_onsets(env)

    diag.bpm = bpm
    diag.confidence = confidence
    diag.beats = int(beats.size)
    diag.sections = len(sections)
    if grid_ok:
        diag.tier = "full"
    elif duration < _MIN_FEATURES_S:
        diag.tier = "unusable"
        diag.reason = (
            f"only {duration:.0f} s analysed (< {_MIN_FEATURES_S:.0f} s) "
            "and no reliable beat grid"
        )
    else:
        diag.tier = "ambient"
        if beats.size < 8:
            diag.reason = (
                f"no beat structure found ({beats.size} beats tracked); "
                "features kept for ambient playback"
            )
        else:
            diag.reason = (
                f"low grid confidence ({confidence:.2f} < {MIN_MAP_CONFIDENCE}): "
                f"beats-on-peaks contrast {diag.contrast:.1f} "
                f"(noise ~2.9, floor {_CONTRAST_FLOOR}), "
                f"interval MAD {diag.mad_local:.2f} vs local tempo, "
                f"coverage {diag.coverage:.0%}, "
                f"tempo stability {stability:.0%}; features kept"
            )
    tm = TrackMap(
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
        onsets=onsets,
        onset_accents=onset_accents,
        diag=diag.as_array(),
    )
    return tm, diag


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
        width=np.asarray(ex.width, dtype=np.float32),  # already 0..1
        pan=_build_pan(ex),
    )


def _build_pan(ex: EnvelopeExtractor) -> np.ndarray:
    """Smoothed, int8-quantised per-frame melbank pan from the L/R means.

    Same ~100 ms symmetric smoothing as the live pan filter, so map replay
    and the live tap agree. Empty when the analysis was mono (analyze_pcm)
    or a side path produced no frames.
    """
    if not ex.mel_l or not ex.mel_r:
        return np.zeros((0, 0), dtype=np.int8)
    n = min(len(ex.mel_l), len(ex.mel_r), len(ex.rms))
    left = np.asarray(ex.mel_l[:n], dtype=np.float32)
    right = np.asarray(ex.mel_r[:n], dtype=np.float32)
    pan = np.clip((right - left) / (right + left + 1e-9), -1.0, 1.0)
    smooth = ExpFilter(
        np.zeros(pan.shape[1], dtype=np.float32), alpha_rise=0.25, alpha_decay=0.25
    )
    for i in range(n):
        pan[i] = smooth.update(pan[i])
    return np.round(pan * 127.0).astype(np.int8)


def _decode_and_analyze(ffmpeg_bin: str, url: str, max_seconds: float) -> MapResult:
    """Blocking: stream-decode ``url`` with ffmpeg and analyse it on the fly.

    Captures ffmpeg's stderr so a server error (auth, 404, transcode failure) is
    surfaced instead of vanishing, and reports how much audio actually decoded so
    the caller can tell a transient fetch failure from an unanalysable track.
    """
    args = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error", *FFMPEG_PROTOCOL_ARGS,
        "-i", url,
        "-t", f"{max_seconds:.0f}",
        # Stereo decode: pan analysis rides alongside; the mid (L+R)/2 feeds
        # the same feature path the old mono decode did.
        "-vn", "-ac", "2", "-ar", str(ANALYSIS_SAMPLE_RATE),
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
            # 256 KiB ~ 1.5 s of stereo audio; a multiple of 8 bytes, so a
            # chunk never splits an interleaved L/R f32 sample pair.
            raw = proc.stdout.read(1 << 18)
            if not raw:
                break
            n_bytes += len(raw)
            samples = np.frombuffer(raw[: len(raw) & ~7], dtype="<f4")
            ex.push_stereo(samples[0::2], samples[1::2])
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
    decoded = (n_bytes / 8 / ANALYSIS_SAMPLE_RATE) >= _MIN_DECODE_S  # 2ch f32 = 8 B/frame
    # "complete" = ffmpeg reached end-of-stream without complaining: only then
    # is an unusable analysis a fact about the MUSIC rather than the fetch, so
    # only then may the caller mark the track permanently failed.
    complete = decoded and not err_txt
    tm, diag = _finish_analysis(ex)
    if tm is not None:
        error = ""
    elif decoded:
        error = diag.reason or "decoded but unanalysable"
    else:
        error = err_txt.splitlines()[-1] if err_txt else "no audio decoded"
    return MapResult(
        track_map=tm, decoded=decoded, error=error,
        complete=complete, diagnostics=diag,
    )


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
        _LOGGER.info("Track-map analysis timed out for %s", redact_url(url))
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
        max_disk: int | None = None,
        track_index: TrackIndex | None = None,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._spawn = spawner or (lambda coro, name: asyncio.create_task(coro, name=name))
        self._cache: OrderedDict[str, TrackMap] = OrderedDict()  # servable maps only
        # In-memory retry bookkeeping for THIS instance's session. Permanence
        # that must survive restarts / be visible across instances lives in the
        # shared TrackIndex; this dict (LRU-bounded) only drives retry backoff.
        self._failures: OrderedDict[str, _Failure] = OrderedDict()
        self._ensuring: set[str] = set()  # track_ids with a disk probe in flight
        self._max_cache = max_cache
        self._task: asyncio.Task | None = None
        self._inflight: str | None = None
        self._stop_prewarm = False
        # Shared persistent per-track outcome record (failures/ambient tiers,
        # labels, the disk budget). Optional so unit tests and standalone use
        # keep working without one.
        self._index = track_index
        # Persistent on-disk cache: a played/prefetched/pre-warmed track's map is
        # written here and reloaded instantly on the next play, so the offline
        # route has no per-track analysis delay once a track has been seen.
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._max_disk = max_disk  # None: follow the index's library-sized budget
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
        return tm if tm is not None and tm.servable else None

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

    @property
    def _disk_budget(self) -> int:
        """The .npz cap: explicit override, else the index's library-sized budget.

        The old fixed 512 silently un-warmed libraries bigger than that — the
        prune evicted pre-warmed maps as fast as the sweep wrote new ones.
        """
        if self._max_disk is not None:
            return self._max_disk
        if self._index is not None:
            return self._index.disk_budget
        return 2048

    def _prune_disk(self) -> None:
        """Keep the disk cache under the budget (evict oldest by mtime)."""
        if self._cache_dir is None:
            return
        budget = self._disk_budget
        try:
            files = sorted(self._cache_dir.glob("*.npz"), key=lambda f: f.stat().st_mtime)
        except OSError:
            return
        for f in files[:-budget] if len(files) > budget else []:
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
        return tm if tm is not None and tm.servable else None

    def failed(self, track_id: str | None) -> bool:
        """True only when ``track_id`` has *permanently* failed analysis.

        While a transient failure is still within its retry budget this returns
        False, so the caller keeps the track-map source (placeholder frames)
        instead of dropping to the metadata animation. Consults this session's
        bookkeeping first, then the shared persistent index — a track the
        library sweep already proved undecodable is not re-decoded at playback,
        and the verdict survives restarts (``retry_failed`` clears it).
        """
        if not track_id:
            return False
        f = self._failures.get(track_id)
        if f is not None:
            return f.permanent
        if self._index is not None:
            rec = self._index.get(track_key(track_id))
            return bool(
                rec and rec.get("status") == "failed" and rec.get("permanent")
            )
        return False

    def ensure(
        self, track_id: str | None, url: str | None, label: str | None = None
    ) -> None:
        """Make a map available for ``track_id``: load from disk, else analyse.

        Called ~1 Hz from the event loop, so the disk read (numpy decompressing
        a cache file) runs in an executor — it must never block the loop. On a
        disk hit the map shows up in :meth:`get` on a later poll (typically the
        very next one); callers already poll, so nothing else changes.
        ``label`` ("Artist - Title") names the track in failure records.
        """
        if not track_id or track_id in self._cache:
            return
        if track_id in self._ensuring:
            return  # disk probe / analysis hand-off already underway
        self._ensuring.add(track_id)
        self._spawn(
            self._ensure_task(track_id, url, label),
            f"hue_music_sync_mapload_{track_id[:24]}",
        )

    async def ensure_ready(
        self, track_id: str | None, url: str | None, label: str | None = None
    ) -> "TrackMap | None":
        """Awaitable :meth:`ensure`: probe the disk cache *now* and return the map.

        For callers that need the answer in-line (``TrackMapSource.open`` deciding
        between track-map and metadata playback for a cached single track). Returns
        the servable map when one is in memory or on disk; otherwise kicks
        background analysis exactly like :meth:`ensure` and returns None.
        """
        if not track_id:
            return None
        tm = self.get(track_id)
        if tm is not None:
            return tm
        if track_id not in self._ensuring:
            self._ensuring.add(track_id)
            try:
                await self._ensure_async(track_id, url, label)
            finally:
                self._ensuring.discard(track_id)
        return self.get(track_id)

    async def _ensure_task(
        self, track_id: str, url: str | None, label: str | None = None
    ) -> None:
        try:
            await self._ensure_async(track_id, url, label)
        finally:
            self._ensuring.discard(track_id)

    async def _ensure_async(
        self, track_id: str, url: str | None, label: str | None = None
    ) -> None:
        if self._index is not None:
            await self._index.ensure_loaded()
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
        if self.failed(track_id):
            return  # permanently failed (this session or the persistent index)
        f = self._failures.get(track_id)
        if f and time.monotonic() < f.retry_at:
            return  # waiting out the retry backoff
        if self._task is not None and not self._task.done():
            return  # one analysis at a time; retried on the next track poll
        self._inflight = track_id
        self._task = self._spawn(
            self._analyze(track_id, url, label),
            f"hue_music_sync_trackmap_{track_id[:24]}",
        )

    def _analysis_lock(self) -> "asyncio.Lock":
        return _global_analysis_lock()

    async def _analyze(self, track_id: str, url: str, label: str | None = None) -> None:
        try:
            async with self._analysis_lock():  # yields to / blocks the pre-warm
                result = await build_track_map(self._ffmpeg, url)
        except Exception:  # noqa: BLE001 - analysis must never break sync
            _LOGGER.debug("Track-map analysis crashed for %s", redact_url(url), exc_info=True)
            result = MapResult(None, decoded=False, error="analysis crashed")
        finally:
            self._inflight = None
        self._record_result(track_id, url, result, label)
        if self._index is not None:
            await self._index.flush()
        # Persist a servable map so the next play is instant (write off the
        # loop — a ~0.3 MB compressed save shouldn't stall the render frames).
        tm = result.track_map
        if tm is not None and tm.servable and self._cache_dir is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._save_disk, track_id, tm
                )
            except Exception:  # noqa: BLE001 - caching must never break sync
                _LOGGER.debug("Track-map disk save failed for %s", track_id, exc_info=True)

    def _record_result(
        self, track_id: str, url: str, result: MapResult, label: str | None = None
    ) -> None:
        tm = result.track_map
        diag = result.diagnostics
        if tm is not None and tm.servable:
            self._failures.pop(track_id, None)
            self._cache[track_id] = tm
            while len(self._cache) > self._max_cache:
                self._cache.popitem(last=False)
            if tm.grid_usable:
                if self._index is not None:
                    self._index.clear(track_key(track_id))
                _LOGGER.info(
                    "Track map ready: %.0f s, %.1f BPM (confidence %.2f), "
                    "%d beats, %d sections",
                    tm.duration, tm.bpm, tm.confidence, tm.beats.size, len(tm.sections),
                )
            else:
                # Ambient tier: full continuous show (features/sections/
                # colours + detected onsets), no scheduled beat grid.
                if self._index is not None:
                    self._index.record(
                        track_key(track_id),
                        label=label,
                        status="ambient",
                        reason=(diag.reason if diag else ""),
                        bpm=tm.bpm,
                        confidence=tm.confidence,
                    )
                _LOGGER.info(
                    "Track map ready (ambient tier, no reliable beat grid): "
                    "%.0f s, %d sections — %s",
                    tm.duration, len(tm.sections),
                    (diag.reason if diag else "low grid confidence"),
                )
            return

        f = self._failures.get(track_id) or _Failure()
        f.attempts += 1
        err = result.error or "unknown error"
        if result.decoded and result.complete:
            # The whole stream decoded cleanly and still produced nothing
            # servable (silence / far too short) — re-analysing won't help.
            # WARNING: this permanently disables track-map sync for the track,
            # which reads as "this song doesn't react" — it must be visible.
            f.permanent = True
            _LOGGER.warning(
                "Track-map analysis produced no usable map for %s (%s); "
                "using live/fallback sync", redact_url(url), err
            )
        elif f.attempts >= _MAX_ANALYSIS_ATTEMPTS:
            f.permanent = True
            _LOGGER.warning(
                "Track-map analysis failed %d times for %s (%s); giving up for "
                "this track", f.attempts, redact_url(url), err
            )
        else:
            delay = min(_RETRY_MAX_S, _RETRY_BASE_S * (2 ** (f.attempts - 1)))
            f.retry_at = time.monotonic() + delay
            _LOGGER.info(
                "Track-map analysis failed for %s (%s); retry %d/%d in %.0fs",
                redact_url(url), err, f.attempts, _MAX_ANALYSIS_ATTEMPTS, delay,
            )
        if self._index is not None:
            self._index.record(
                track_key(track_id),
                label=label,
                status="failed" if f.permanent else "retrying",
                reason=err,
                attempts=f.attempts,
                permanent=f.permanent,
            )
        self._failures[track_id] = f
        self._failures.move_to_end(track_id)
        while len(self._failures) > self._max_cache * 2:
            self._failures.popitem(last=False)

    async def analyze_now(
        self,
        track_id: str | None,
        url: str,
        label: str | None = None,
        force: bool = True,
    ) -> MapResult:
        """One-shot, awaited analysis of a single track (the diagnose service).

        ``force`` clears any failure verdict and re-analyses even when a map is
        already cached — the per-track tool for "why doesn't this song react"
        and for re-checking a track after an analysis upgrade. The result (and
        its :class:`MapDiagnostics`) is returned for the caller to surface;
        a servable map is cached/persisted exactly like any other analysis.
        """
        if self._index is not None:
            await self._index.ensure_loaded()
        if track_id and force:
            self._failures.pop(track_id, None)
            self._cache.pop(track_id, None)
            if self._index is not None:
                self._index.clear(track_key(track_id))
        try:
            async with self._analysis_lock():
                result = await build_track_map(self._ffmpeg, url)
        except Exception:  # noqa: BLE001 - diagnostics must never crash HA
            _LOGGER.debug(
                "One-shot analysis crashed for %s", redact_url(url), exc_info=True
            )
            result = MapResult(None, decoded=False, error="analysis crashed")
        if track_id:
            self._record_result(track_id, url, result, label)
            tm = result.track_map
            if tm is not None and tm.servable and self._cache_dir is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._save_disk, track_id, tm
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "One-shot disk save failed for %s", track_id, exc_info=True
                    )
            if self._index is not None:
                await self._index.flush(force=True)
        return result

    async def prewarm(
        self,
        items,
        *,
        delay_s: float = 1.0,
        progress=None,
    ) -> PrewarmResult:
        """Analyse + cache every uncached ``(track_id, url, label)`` one at a time.

        A gentle, resumable background sweep of a music library so a track plays
        instantly (offline track-map) the first time too. Already-cached tracks
        (in memory or on disk) and permanently-failed ones are skipped, so a
        re-run continues where it left off — and picks up newly-added library
        tracks cheaply. The shared analysis lock means a live playback analysis
        takes priority — the pre-warm waits between tracks and never decodes two
        streams at once. Returns a :class:`PrewarmResult`; a systematically
        failing library (bad login, wrong URL scheme) must be visible, not
        silent, so failures carry labelled samples and every outcome lands in
        the shared TrackIndex.
        """
        self._stop_prewarm = False
        if self._index is not None:
            await self._index.ensure_loaded()
        res = PrewarmResult()
        for item in items:
            track_id, url, label = (
                item if len(item) >= 3 else (item[0], item[1], None)
            )
            if self._stop_prewarm:
                break
            res.considered += 1
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
                    _LOGGER.debug("Pre-warm analysis crashed for %s", redact_url(url), exc_info=True)
                    result = MapResult(None, decoded=False, error="prewarm crashed")
                self._record_result(track_id, url, result, label)
                tm = result.track_map
                if tm is not None and tm.servable:
                    if self._cache_dir is not None:
                        try:
                            await asyncio.get_running_loop().run_in_executor(
                                None, self._save_disk, track_id, tm
                            )
                        except Exception:  # noqa: BLE001
                            _LOGGER.debug(
                                "Pre-warm disk save failed for %s", track_id, exc_info=True
                            )
                    if tm.grid_usable:
                        res.analysed += 1
                    else:
                        res.ambient += 1
                else:
                    res.failed += 1
                    err = result.error or (
                        "decoded but unanalysable" if result.decoded else "unknown error"
                    )
                    if len(res.failures) < 20:
                        res.failures.append((label or "?", url, err))
                    if res.failed <= 10:  # first few at WARNING; a broken library must show
                        _LOGGER.warning(
                            "Library pre-warm: analysis failed for %s [%s] (%s)",
                            label or "?", redact_url(url), err,
                        )
                    elif res.failed == 11:
                        _LOGGER.warning(
                            "Library pre-warm: more analysis failures; further ones "
                            "logged at debug level only"
                        )
                if self._index is not None:
                    await self._index.flush()  # debounced
                await asyncio.sleep(delay_s)  # be gentle on CPU + the music library
            finally:
                # Every item counts as progress (cached/skipped ones too), so a
                # progress surface moves smoothly instead of stalling on a
                # mostly-cached library.
                if progress is not None:
                    progress(res.analysed + res.ambient, res.considered)
        if self._index is not None:
            await self._index.flush(force=True)
        return res

    def stop_prewarm(self) -> None:
        """Ask an in-flight :meth:`prewarm` sweep to stop after the current track."""
        self._stop_prewarm = True

    async def close(self) -> None:
        self._stop_prewarm = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._index is not None:
            await self._index.flush(force=True)
