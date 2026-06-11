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
import logging
import subprocess
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field

import numpy as np

from ..const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, ANALYSIS_WINDOW
from .analyzer import (
    band_means,
    log_spectrum,
    make_onset_filterbank,
    max_filter_freq,
)
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

_MAX_TRACK_S = 720.0  # analysis cap (12 min); beyond this fall back to live
_DECODE_TIMEOUT_S = 90.0  # ffmpeg must finish well before the track does


@dataclass(slots=True)
class Section:
    start: float
    end: float
    energy: float  # 0..1 loudness of this section relative to the track's peak


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

    @property
    def usable(self) -> bool:
        return self.confidence >= MIN_MAP_CONFIDENCE and self.beats.size >= 8

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
        self._fb_starts, self._fb_counts, self.n_bass = make_onset_filterbank(
            freqs.astype(np.float32)
        )
        self._tail = np.zeros(0, dtype=np.float32)
        self._prev_log: np.ndarray | None = None
        self.env: list[float] = []  # SuperFlux onset envelope per frame
        self.bass_env: list[float] = []  # bass-band log-flux per frame
        self.rms: list[float] = []
        self.bands: list[np.ndarray] = []  # linear filterbank means per frame

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
        diff = logb - max_filter_freq(prev)
        np.maximum(diff, 0.0, out=diff)
        self.env.extend(np.sum(diff, axis=1).tolist())
        bdiff = logb[:, : self.n_bass] - prev[:, : self.n_bass]
        np.maximum(bdiff, 0.0, out=bdiff)
        self.bass_env.extend(np.sum(bdiff, axis=1).tolist())
        self.rms.extend(np.sqrt(np.mean(frames * frames, axis=1)).tolist())
        self.bands.extend(lin)
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
    bpm = float(cand_bpm[best])
    confidence = float(max(0.0, min(1.0, seg[best] / zero)))
    return bpm, confidence


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

    # Peak-pick boundaries: local maxima above mean+std, min section length.
    thresh = novelty.mean() + novelty.std()
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
    accents_raw = env[np.minimum(beat_frames, env.size - 1)]
    peak = float(accents_raw.max()) or 1.0
    accents = (accents_raw / peak).astype(np.float64)
    downbeat = _find_downbeat(bass_env, beat_frames)
    # Refine the bpm from the actual tracked beats (more honest than the lag).
    intervals = np.diff(beats)
    if intervals.size:
        bpm = float(60.0 / np.median(intervals))
    sections = _segment_sections(bands, rms, duration)
    return TrackMap(
        duration=duration,
        bpm=bpm,
        confidence=confidence,
        beats=beats,
        accents=accents,
        downbeat=downbeat,
        sections=sections,
    )


def _decode_and_analyze(ffmpeg_bin: str, url: str, max_seconds: float) -> TrackMap | None:
    """Blocking: stream-decode ``url`` with ffmpeg and analyse it on the fly."""
    args = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error",
        "-i", url,
        "-t", f"{max_seconds:.0f}",
        "-vn", "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE),
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError as err:
        _LOGGER.debug("Track-map decode failed to start: %s", err)
        return None
    ex = EnvelopeExtractor()
    try:
        assert proc.stdout is not None
        while True:
            raw = proc.stdout.read(1 << 18)  # 256 KiB ~ 3 s of audio
            if not raw:
                break
            ex.push(np.frombuffer(raw, dtype="<f4"))
    finally:
        try:
            proc.stdout.close()  # type: ignore[union-attr]
        except OSError:
            pass
        proc.kill()
        proc.wait()
    return _finish_analysis(ex)


async def build_track_map(
    ffmpeg_bin: str, url: str, max_seconds: float = _MAX_TRACK_S
) -> TrackMap | None:
    """Decode + analyse a track in an executor, bounded by a timeout."""
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _decode_and_analyze, ffmpeg_bin, url, max_seconds),
            timeout=_DECODE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _LOGGER.info("Track-map analysis timed out for %s", url)
        return None


class TrackMapper:
    """Background per-track analysis with a small in-memory cache.

    HA-free; the caller supplies a task spawner (e.g. Home Assistant's
    ``async_create_background_task``) so analysis never blocks the render loop.
    """

    def __init__(
        self,
        ffmpeg_bin: str,
        spawner: Callable[[Coroutine, str], asyncio.Task] | None = None,
        max_cache: int = 12,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._spawn = spawner or (lambda coro, name: asyncio.create_task(coro, name=name))
        self._cache: OrderedDict[str, TrackMap | None] = OrderedDict()
        self._max_cache = max_cache
        self._task: asyncio.Task | None = None
        self._inflight: str | None = None

    def get(self, track_id: str | None) -> TrackMap | None:
        if not track_id:
            return None
        tm = self._cache.get(track_id)
        return tm if tm is not None and tm.usable else None

    def ensure(self, track_id: str | None, url: str | None) -> None:
        """Kick off analysis for ``track_id`` if it isn't cached or running."""
        if not track_id or not url or track_id in self._cache:
            return
        if self._task is not None and not self._task.done():
            return  # one analysis at a time; retried on the next track poll
        self._inflight = track_id
        self._task = self._spawn(
            self._analyze(track_id, url), f"hue_music_sync_trackmap_{track_id[:24]}"
        )

    async def _analyze(self, track_id: str, url: str) -> None:
        try:
            tm = await build_track_map(self._ffmpeg, url)
        except Exception:  # noqa: BLE001 - analysis must never break sync
            _LOGGER.debug("Track-map analysis crashed for %s", url, exc_info=True)
            tm = None
        finally:
            self._inflight = None
        # Cache failures too (as None) so a bad URL isn't re-fetched every poll.
        self._cache[track_id] = tm
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)
        if tm is not None:
            _LOGGER.info(
                "Track map ready: %.0f s, %.1f BPM (confidence %.2f), "
                "%d beats, %d sections",
                tm.duration, tm.bpm, tm.confidence, tm.beats.size, len(tm.sections),
            )

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
