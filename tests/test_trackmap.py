"""Offline track map: DP beats, downbeats, sections and the scheduled grid."""

from __future__ import annotations

import time

import numpy as np
import pytest

from hue_music_sync.audio.trackmap import (
    _MAX_ANALYSIS_ATTEMPTS,
    MapResult,
    TrackMap,
    TrackMapper,
    analyze_pcm,
)
from hue_music_sync.const import ANALYSIS_SAMPLE_RATE

_SR = ANALYSIS_SAMPLE_RATE


def _kick(env_len: int, freq: float = 60.0) -> np.ndarray:
    env = np.exp(-np.arange(env_len) / (0.03 * _SR))
    return (np.sin(2 * np.pi * freq * np.arange(env_len) / _SR) * env).astype(np.float32)


def _song(
    bpm: float = 120.0,
    seconds: float = 40.0,
    quiet_until: float | None = None,
    amp_quiet: float = 0.25,
) -> np.ndarray:
    """Kick track with optional quiet-intro/loud-chorus structure."""
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    period = 60.0 / bpm
    kick = _kick(int(0.1 * _SR))
    for bt in np.arange(0.30, seconds, period):
        i = int(bt * _SR)
        amp = amp_quiet if (quiet_until is not None and bt < quiet_until) else 1.0
        seg = kick[: len(sig) - i] * amp
        sig[i : i + len(seg)] += seg
    if quiet_until is not None:
        # The "chorus" adds sustained harmonic content (energy + brightness).
        t0 = int(quiet_until * _SR)
        t = np.arange(len(sig) - t0) / _SR
        pad = 0.25 * (np.sin(2 * np.pi * 220 * t) + 0.6 * np.sin(2 * np.pi * 880 * t))
        sig[t0:] += pad.astype(np.float32)
    peak = np.max(np.abs(sig))
    return sig / peak * 0.9 if peak else sig


@pytest.fixture(scope="module")
def map_120() -> TrackMap:
    tm = analyze_pcm(_song(bpm=120.0, seconds=40.0))
    assert tm is not None
    return tm


def test_offline_tempo_is_exact(map_120: TrackMap):
    assert map_120.usable
    assert abs(map_120.bpm - 120.0) < 2.0
    assert map_120.confidence > 0.3


def test_beats_land_on_the_kicks(map_120: TrackMap):
    true_beats = np.arange(0.30, 40.0, 0.5)
    # For every tracked beat (away from the edges) the nearest kick is close.
    checked = 0
    for b in map_120.beats:
        if b < 1.0 or b > 38.0:
            continue
        err = np.min(np.abs(true_beats - b))
        assert err < 0.06, f"beat at {b:.3f}s is {err*1000:.0f}ms off"
        checked += 1
    assert checked > 50  # most of the song's beats were tracked


def test_grid_at_schedules_the_next_beat(map_120: TrackMap):
    g = map_120.grid_at(10.02)
    assert g is not None and g.locked
    assert abs(g.bpm - 120.0) < 3.0
    assert 0.0 <= g.time_to_next_beat <= g.period_s + 1e-6
    # Crossing a beat sets predicted_beat exactly once.
    crossings = 0
    prev = 10.0
    for pos in np.arange(10.0, 12.0, 0.02):
        g = map_120.grid_at(float(pos), prev)
        prev = float(pos)
        if g is not None and g.predicted_beat:
            crossings += 1
    assert crossings == 4  # 2 seconds at 120 BPM


def test_grid_outside_track_is_none(map_120: TrackMap):
    assert map_120.grid_at(-30.0) is None
    assert map_120.grid_at(10_000.0) is None


def test_sections_split_quiet_intro_from_loud_chorus():
    tm = analyze_pcm(_song(bpm=124.0, seconds=60.0, quiet_until=30.0))
    assert tm is not None and tm.usable
    assert len(tm.sections) >= 2
    # A boundary lands near 30 s and the later section is clearly louder.
    boundary = min((s.start for s in tm.sections[1:]), key=lambda x: abs(x - 30.0))
    assert abs(boundary - 30.0) < 6.0
    early = tm.section_at(10.0)
    late = tm.section_at(45.0)
    assert early is not None and late is not None
    assert late.energy > early.energy + 0.2


def test_features_are_stored_and_normalised(map_120: TrackMap):
    f = map_120.features
    assert f is not None
    n = f.energy.shape[0]
    assert f.bands.shape == (n, 5)
    assert f.flux.shape == (n,) and f.bass_flux.shape == (n,) and f.centroid.shape == (n,)
    for arr in (f.bands, f.energy, f.flux, f.bass_flux, f.centroid):
        assert float(arr.min()) >= 0.0 and float(arr.max()) <= 1.0
    # The kicks are 60 Hz: loud frames carry their energy in the low bands.
    loud = f.bands[f.energy > 0.5]
    assert loud.size and float(loud[:, :2].max()) > 0.5  # sub_bass/bass active
    assert float(loud[:, 4].mean()) < 0.3  # not treble


def test_frame_at_replays_beats_exactly_once(map_120: TrackMap):
    # Sweeping the position forward fires bass_beat exactly once per map beat.
    beats_in_window = np.sum((map_120.beats >= 10.0) & (map_120.beats < 14.0))
    fired = 0
    prev = 10.0
    for pos in np.arange(10.0, 14.0, 0.02):
        fr = map_120.frame_at(float(pos), prev)
        prev = float(pos)
        assert fr is not None
        if fr.bass_beat:
            fired += 1
            assert fr.bass_strength >= 1.0  # accent-scaled like the live path
    assert fired == beats_in_window


def test_frame_at_bands_follow_the_audio(map_120: TrackMap):
    fr = map_120.frame_at(12.0)
    assert fr is not None
    assert set(fr.bands) == {"sub_bass", "bass", "low_mid", "mid", "high"}
    assert fr.t_audio == 12.0
    assert map_120.frame_at(-5.0) is None
    assert map_120.frame_at(10_000.0) is None


def test_map_schedules_mid_onsets_and_accents():
    # Kicks + off-beat 700 Hz plucks: the map must schedule the plucks on the
    # mid stream, and grid_at must expose a per-beat accent for wave sizing.
    seconds = 30.0
    sig = _song(bpm=120.0, seconds=seconds)
    sr = _SR
    pluck = _kick(int(0.06 * sr), freq=700.0)
    for bt in np.arange(0.55, seconds, 1.0):
        i = int(bt * sr)
        seg = pluck[: len(sig) - i] * 0.8
        sig[i : i + len(seg)] += seg
    sig = sig / np.max(np.abs(sig)) * 0.9
    tm = analyze_pcm(sig)
    assert tm is not None and tm.usable
    assert tm.mid_beats.size >= 10
    # Most scheduled mid onsets land near the true pluck times.
    true = np.arange(0.55, seconds, 1.0)
    close = sum(1 for t in tm.mid_beats if np.min(np.abs(true - t)) < 0.08)
    assert close >= tm.mid_beats.size * 0.7
    g = tm.grid_at(10.0)
    assert g is not None and 0.0 <= g.accent <= 1.0
    fr = tm.frame_at(10.0)
    assert fr is not None and fr.mid_flux >= 0.0


def test_noise_is_not_usable():
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(_SR * 20) * 0.3).astype(np.float32)
    tm = analyze_pcm(noise)
    assert tm is None or not tm.usable


def test_silence_returns_none():
    assert analyze_pcm(np.zeros(_SR * 2, dtype=np.float32)) is None


# --- TrackMapper retry / failure policy -----------------------------------

class _FakeTask:
    def done(self) -> bool:
        return True


def _spawner(recorder: list[str]):
    def spawn(coro, name):
        coro.close()  # don't actually run analysis in these unit tests
        recorder.append(name)
        return _FakeTask()
    return spawn


def test_transient_failure_is_retried_then_permanent():
    m = TrackMapper("ffmpeg")
    tid = "navidrome:1"
    # A decode/network failure (no audio) is transient: retryable, not failed.
    m._record_result(tid, "u", MapResult(None, decoded=False, error="busy"))
    assert not m.failed(tid)
    assert m.get(tid) is None
    f = m._failures[tid]
    assert f.attempts == 1 and not f.permanent and f.retry_at > time.monotonic()
    # Exhausting the retry budget marks it permanently failed.
    for _ in range(_MAX_ANALYSIS_ATTEMPTS):
        m._record_result(tid, "u", MapResult(None, decoded=False, error="busy"))
    assert m.failed(tid)


def test_decoded_but_unusable_is_permanent_immediately():
    m = TrackMapper("ffmpeg")
    # Audio decoded fine but no usable beat map: re-analysis won't help.
    m._record_result("t", "u", MapResult(None, decoded=True))
    assert m.failed("t")


def test_success_caches_map_and_clears_prior_failure():
    tm = analyze_pcm(_song(bpm=120.0, seconds=40.0))
    assert tm is not None and tm.usable
    m = TrackMapper("ffmpeg")
    m._record_result("t", "u", MapResult(None, decoded=False))  # one transient miss
    assert "t" in m._failures and not m.failed("t")
    m._record_result("t", "u", MapResult(tm, decoded=True))     # then it succeeds
    assert m.get("t") is tm
    assert not m.failed("t") and "t" not in m._failures


def test_ensure_respects_backoff_and_permanence():
    spawned: list[str] = []
    m = TrackMapper("ffmpeg", spawner=_spawner(spawned))
    m._record_result("t", "u", MapResult(None, decoded=False))
    m._failures["t"].retry_at = time.monotonic() + 100  # still backing off
    m.ensure("t", "u")
    assert spawned == []  # must not re-spawn before the backoff elapses
    m._failures["t"].retry_at = time.monotonic() - 1  # backoff elapsed
    m.ensure("t", "u")
    assert spawned == ["hue_music_sync_trackmap_t"]
    spawned.clear()
    m._failures["t"].permanent = True  # given up: never retried again
    m.ensure("t", "u")
    assert spawned == []
