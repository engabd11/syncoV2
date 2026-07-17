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


# --- persistent disk cache -------------------------------------------------

def test_track_map_save_load_round_trips(map_120: TrackMap, tmp_path):
    path = tmp_path / "m.npz"
    map_120.save(path)
    assert path.exists()
    back = TrackMap.load(path)
    assert back is not None and back.usable
    assert back.bpm == pytest.approx(map_120.bpm)
    assert back.downbeat == map_120.downbeat
    np.testing.assert_allclose(back.beats, map_120.beats)
    np.testing.assert_allclose(back.accents, map_120.accents)
    assert (back.features is None) == (map_120.features is None)
    if map_120.features is not None:
        np.testing.assert_allclose(back.features.energy, map_120.features.energy)
        np.testing.assert_allclose(back.features.melbank, map_120.features.melbank)
        np.testing.assert_allclose(back.features.width, map_120.features.width)
    assert len(back.sections) == len(map_120.sections)
    # A reloaded map drives playback identically (same scheduled frame).
    a = map_120.frame_at(5.0, 4.9)
    b = back.frame_at(5.0, 4.9)
    assert a is not None and b is not None
    assert a.beat == b.beat and a.beat_strength == pytest.approx(b.beat_strength)


def _rewrite_format(src, dst, fmt: float, drop: tuple = ()):
    """Re-write a saved map as an older/other on-disk format for compat tests."""
    with np.load(src) as z:
        data = {k: z[k] for k in z.files if k not in drop}
    data["meta"] = data["meta"].copy()
    data["meta"][4] = fmt
    np.savez_compressed(dst, **data)


def test_load_accepts_v2_and_rejects_unknown_formats(map_120: TrackMap, tmp_path):
    # The explicit compat policy: v2 files (pre-ambient-tier, no onsets/diag
    # rows) still load — a format bump must NOT silently un-warm a
    # pre-analysed library — while an unknown (newer) format is ignored
    # rather than mis-read.
    src = tmp_path / "m.npz"
    map_120.save(src)

    v2 = tmp_path / "v2.npz"
    _rewrite_format(
        src, v2, 2.0,
        drop=("onsets", "onset_accents", "diag",
              "sec_group", "mom_kind", "mom_t0", "mom_t1", "mom_strength"),
    )
    back = TrackMap.load(v2)
    assert back is not None and back.usable  # v2 accepted
    # Missing v3/v5 rows default safely: diag is zero-padded to the full
    # 9-entry layout, moments stay empty and sections stay ungrouped.
    assert back.diag.shape == (9,)
    assert not back.diag.any()
    assert back.mom_kind.size == 0
    assert all(s.group == -1 for s in back.sections)

    unknown = tmp_path / "v99.npz"
    _rewrite_format(src, unknown, 99.0)
    assert TrackMap.load(unknown) is None  # unknown version → ignored


def test_load_handles_a_corrupt_file(tmp_path):
    bad = tmp_path / "bad.npz"
    bad.write_bytes(b"not an npz")
    assert TrackMap.load(bad) is None


def test_mapper_loads_from_disk_without_reanalysing(map_120: TrackMap, tmp_path):
    # Pre-seed the disk cache, then a fresh mapper must serve the map from disk
    # via ensure() (an off-loop disk probe) and NEVER spawn an analysis task.
    import asyncio

    spawned = []
    mapper = TrackMapper(
        "ffmpeg",
        spawner=lambda coro, name: spawned.append((name, coro)) or _DummyTask(),
        cache_dir=tmp_path,
    )
    tid = "artist|title|1"
    mapper._save_disk(tid, map_120)
    assert mapper.has_disk(tid)
    assert mapper.get(tid) is None  # not in memory yet
    mapper.ensure(tid, "http://example/stream")
    [(name, coro)] = spawned  # exactly one task: the disk probe, not analysis
    assert "mapload" in name
    asyncio.run(coro)  # run the probe; a disk hit must not chain an analysis
    assert len(spawned) == 1  # crucially: no analysis was kicked off
    assert mapper.get(tid) is not None  # now served from memory


def test_prewarm_analyses_and_caches_then_skips_on_rerun(map_120: TrackMap, tmp_path, monkeypatch):
    # The library pre-warm analyses every uncached (track_id, url) and writes it
    # to disk; a re-run skips the now-cached tracks (resumable, no re-analysis).
    import asyncio

    import hue_music_sync.audio.trackmap as tmmod

    seen: list[str] = []

    async def fake_build(ffmpeg, url, max_seconds=tmmod._MAX_TRACK_S):
        seen.append(url)
        return MapResult(track_map=map_120, decoded=True)

    monkeypatch.setattr(tmmod, "build_track_map", fake_build)
    items = [
        ("artist|a|1", "http://lib/1", "Artist - A"),
        ("artist|b|2", "http://lib/2", "Artist - B"),
    ]

    mapper = TrackMapper("ffmpeg", cache_dir=tmp_path)
    res = asyncio.run(mapper.prewarm(items, delay_s=0))
    assert (res.analysed, res.considered, res.failures) == (2, 2, [])
    assert mapper.has_disk("artist|a|1") and mapper.has_disk("artist|b|2")
    assert seen == ["http://lib/1", "http://lib/2"]

    # A fresh mapper over the same library: everything is already on disk.
    seen.clear()
    mapper2 = TrackMapper("ffmpeg", cache_dir=tmp_path)
    res2 = asyncio.run(mapper2.prewarm(items, delay_s=0))
    assert (res2.analysed, res2.considered, res2.failures) == (0, 2, [])
    assert seen == []  # nothing re-analysed


def test_prewarm_reports_failures(map_120: TrackMap, tmp_path, monkeypatch):
    # A systematically failing library (bad login, wrong URL) must be VISIBLE:
    # prewarm returns labelled (label, url, error) samples instead of failing
    # silently — the user needs to see WHICH songs failed.
    import asyncio

    import hue_music_sync.audio.trackmap as tmmod

    async def fake_build(ffmpeg, url, max_seconds=tmmod._MAX_TRACK_S):
        if url.endswith("/bad"):
            return MapResult(None, decoded=False, error="HTTP 401")
        return MapResult(track_map=map_120, decoded=True)

    monkeypatch.setattr(tmmod, "build_track_map", fake_build)
    items = [
        ("t|good|1", "http://lib/good", "T - Good"),
        ("t|bad|2", "http://lib/bad", "T - Bad"),
    ]
    mapper = TrackMapper("ffmpeg", cache_dir=tmp_path)
    res = asyncio.run(mapper.prewarm(items, delay_s=0))
    assert (res.analysed, res.considered, res.failed) == (1, 2, 1)
    assert res.failures == [("T - Bad", "http://lib/bad", "HTTP 401")]


def test_ensure_loads_from_disk_even_without_a_url(map_120: TrackMap, tmp_path):
    # The single-track fix: a previously-played track is cached on disk, so even
    # with NO fresh per-track URL (a single ad-hoc track MA won't expose a stream
    # for), ensure() serves it from disk instead of failing to the metadata
    # fallback — TrackMapSource.open() then succeeds and the lights react.
    import asyncio

    spawned = []
    mapper = TrackMapper(
        "ffmpeg",
        spawner=lambda coro, name: spawned.append((name, coro)) or _DummyTask(),
        cache_dir=tmp_path,
    )
    tid = "single|track|1"
    mapper._save_disk(tid, map_120)
    mapper.ensure(tid, None)  # no URL at all
    [(name, coro)] = spawned  # just the disk probe
    asyncio.run(coro)
    assert mapper.get(tid) is not None  # served from disk
    assert len(spawned) == 1  # and no analysis needed


def test_ensure_ready_serves_cached_map_inline(map_120: TrackMap, tmp_path):
    # TrackMapSource.open() awaits ensure_ready() and needs the cached map in
    # the SAME call — a cached single track must open as track-map playback,
    # not fall to the metadata animation until a later poll finds the map.
    import asyncio

    spawned = []
    mapper = TrackMapper(
        "ffmpeg",
        spawner=lambda coro, name: spawned.append((name, coro)) or _DummyTask(),
        cache_dir=tmp_path,
    )
    tid = "single|track|2"
    mapper._save_disk(tid, map_120)
    tm = asyncio.run(mapper.ensure_ready(tid, None))
    assert tm is not None and tm.usable
    assert spawned == []  # disk hit resolved inline: no probe, no analysis
    assert mapper.get(tid) is not None


def test_ensure_ready_kicks_analysis_when_uncached(map_120: TrackMap, tmp_path):
    import asyncio

    spawned = []
    mapper = TrackMapper(
        "ffmpeg",
        spawner=lambda coro, name: spawned.append((name, coro)) or _DummyTask(),
        cache_dir=tmp_path,
    )
    tm = asyncio.run(mapper.ensure_ready("never|seen|1", "http://lib/x"))
    assert tm is None  # not ready yet — analysis runs in the background
    [(name, coro)] = spawned
    assert "trackmap" in name  # the analysis task was chained
    coro.close()


class _DummyTask:
    def __init__(self, coro=None):
        if coro is not None:
            coro.close()  # we never run it; just don't leak the coroutine

    def done(self):
        return True


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


def test_steady_groove_rescued_from_diluted_autocorr_confidence(monkeypatch):
    # The real-world failure (BALTHVS "Ojos Verdes", measured live): a
    # perfectly steady groove whose normalised autocorrelation confidence
    # (0.29) fell a hair under the 0.30 usability gate, permanently sending
    # the song to the dead metadata animation. The grid rescue must lift it:
    # dense regular beats landing on strong envelope peaks ARE the evidence.
    import hue_music_sync.audio.trackmap as tmmod

    real = tmmod._estimate_tempo

    def diluted(env):
        bpm, _conf = real(env)
        return bpm, 0.29  # simulate the dynamics-diluted confidence

    monkeypatch.setattr(tmmod, "_estimate_tempo", diluted)
    tm = analyze_pcm(_song(bpm=120.0, seconds=40.0))
    assert tm is not None
    assert tm.confidence > 0.30 and tm.usable


def test_noise_is_not_rescued():
    # The rescue must not resurrect noise: the DP tracker regularises beat
    # intervals even on noise, so the discriminator is beats-on-peaks contrast
    # (measured ~2.9 on noise vs 4.2+ on real grooves) — below the floor, the
    # diluted confidence stays as-is and the map stays unusable.
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(_SR * 45) * 0.3).astype(np.float32)
    tm = analyze_pcm(noise)
    assert tm is None or not tm.usable


# --- TrackMapper retry / failure policy -----------------------------------

class _FakeTask:
    def done(self) -> bool:
        return True


def _spawner(recorder: list):
    def spawn(coro, name):
        recorder.append((name, coro))
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
    # Audio decoded fine (whole stream, cleanly) but nothing servable came out
    # (silence / far too short): re-analysis won't help.
    m._record_result("t", "u", MapResult(None, decoded=True, complete=True))
    assert m.failed("t")


def test_partial_decode_is_transient():
    # A decode that produced >5 s of audio but did NOT reach a clean end of
    # stream (network drop, server hiccup) may analyse fine next time — it
    # must be retried, not permanently failed.
    m = TrackMapper("ffmpeg")
    m._record_result("t", "u", MapResult(None, decoded=True, complete=False))
    assert not m.failed("t")
    assert m._failures["t"].attempts == 1 and not m._failures["t"].permanent


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
    import asyncio

    spawned: list = []

    def run_probe() -> list[str]:
        # ensure() spawns an off-loop disk probe; drive it and report the names
        # of anything it chained (the analysis task, if any), without actually
        # running an analysis in these unit tests.
        name, coro = spawned.pop(0)
        assert "mapload" in name
        asyncio.run(coro)
        names = [n for n, _ in spawned]
        for _, chained in spawned:
            chained.close()
        spawned.clear()
        return names

    m = TrackMapper("ffmpeg", spawner=_spawner(spawned))
    m._record_result("t", "u", MapResult(None, decoded=False))
    m._failures["t"].retry_at = time.monotonic() + 100  # still backing off
    m.ensure("t", "u")
    assert run_probe() == []  # must not re-spawn before the backoff elapses
    m._failures["t"].retry_at = time.monotonic() - 1  # backoff elapsed
    m.ensure("t", "u")
    assert run_probe() == ["hue_music_sync_trackmap_t"]
    m._failures["t"].permanent = True  # given up: never retried again
    m.ensure("t", "u")
    assert run_probe() == []


# --- event-selection evidence on the map path (salience + onset width) -------

def test_features_carry_width_and_frame_at_reports_evidence(map_120: TrackMap):
    f = map_120.features
    assert f is not None
    assert f.width.shape[0] == f.energy.shape[0]  # per-frame, same length
    assert float(f.width.max()) <= 1.0 and float(f.width.min()) >= 0.0
    fr = map_120.frame_at(5.0, 4.9)
    assert fr is not None
    i = int(5.0 / 0.02)
    assert fr.salience == pytest.approx(float(f.energy[i]))
    assert fr.onset_width == pytest.approx(float(f.width[i]))


def test_map_salience_is_proportional_across_quiet_and_loud_sections():
    tm = analyze_pcm(_song(bpm=120.0, seconds=40.0, quiet_until=20.0))
    assert tm is not None and tm.features is not None
    e = tm.features.energy
    n = int(20.0 / 0.02)
    quiet = float(np.percentile(e[100:n], 95))
    loud = float(np.percentile(e[n + 100:], 95))
    assert loud > quiet + 0.2  # the quiet intro replays proportionally dimmer


def test_width_filter_drops_narrowband_picks():
    from hue_music_sync.audio.trackmap import _FRAME_PERIOD, _width_filter

    width = np.full(1000, 0.5)
    width[490:520] = 0.03  # a narrowband (vocal-like) stretch
    times = np.array([100, 300, 505]) * _FRAME_PERIOD
    accents = np.array([0.5, 0.6, 0.9])
    t2, a2 = _width_filter(times, accents, width)
    np.testing.assert_allclose(t2, times[:2])
    np.testing.assert_allclose(a2, accents[:2])


# --- ambient (features-only) tier: a decodable track never falls to metadata --

def _gridless(map_120: TrackMap) -> TrackMap:
    """A copy of a real map with the grid rejected (the ambient tier)."""
    from dataclasses import replace

    return replace(
        map_120,
        confidence=0.10,  # below MIN_MAP_CONFIDENCE -> grid_usable False
        onsets=map_120.beats.copy(),  # detected onsets stand in for the grid
        onset_accents=map_120.accents.copy(),
    )


def test_gridless_map_is_servable_but_grid_at_is_none(map_120: TrackMap):
    amb = _gridless(map_120)
    assert not amb.grid_usable and amb.features_usable and amb.servable
    assert amb.grid_at(10.0) is None  # never claims a schedule
    fr = amb.frame_at(12.0)
    assert fr is not None
    assert fr.tempo_bpm == 0.0  # no trusted tempo reported
    # ...but the continuous show is fully fed (the fixture is sparse kicks, so
    # sample across a window rather than one frame).
    peak = max(
        f.energy for p in np.arange(10.0, 12.0, 0.02)
        if (f := amb.frame_at(float(p))) is not None
    )
    assert peak > 0.5


def test_gridless_map_replays_detected_onsets(map_120: TrackMap):
    # The ambient tier fires its DETECTED onsets (honest events) so the
    # engine's unlocked flash path works and the causal tracker gets phase
    # anchors — exactly once each, like the scheduled path.
    amb = _gridless(map_120)
    expect = np.sum((amb.onsets >= 10.0) & (amb.onsets < 14.0))
    fired = 0
    prev = 10.0
    for pos in np.arange(10.0, 14.0, 0.02):
        fr = amb.frame_at(float(pos), prev)
        prev = float(pos)
        assert fr is not None
        if fr.beat:
            fired += 1
            assert fr.beat_strength >= 1.0
    assert fired == expect > 0


def test_ambient_map_round_trips_via_disk(map_120: TrackMap, tmp_path):
    amb = _gridless(map_120)
    path = tmp_path / "amb.npz"
    amb.save(path)
    back = TrackMap.load(path)
    assert back is not None and back.servable and not back.grid_usable
    np.testing.assert_allclose(back.onsets, amb.onsets)
    np.testing.assert_allclose(back.diag, amb.diag)


def test_ambient_map_is_served_not_failed(map_120: TrackMap, tmp_path):
    # THE headline fix: decoded-but-gridless used to be a permanent failure
    # (metadata animation forever). Now it is cached, persisted and served.
    amb = _gridless(map_120)
    m = TrackMapper("ffmpeg", cache_dir=tmp_path)
    m._record_result("t", "u", MapResult(amb, decoded=True, complete=True))
    assert not m.failed("t")
    assert m.get("t") is amb


def test_long_noise_lands_in_ambient_tier_not_unusable():
    # 45 s of noise: no trustworthy grid (contrast gate), but the features are
    # honest — the lights follow the audio instead of a dead animation.
    from hue_music_sync.audio.trackmap import analyze_pcm_diag

    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(_SR * 45) * 0.3).astype(np.float32)
    tm, diag = analyze_pcm_diag(noise)
    assert tm is not None and not tm.grid_usable and tm.servable
    assert diag.tier == "ambient" and diag.reason


def test_short_gridless_audio_is_unusable():
    from hue_music_sync.audio.trackmap import analyze_pcm_diag

    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(int(_SR * 10)) * 0.3).astype(np.float32)
    tm, diag = analyze_pcm_diag(noise)
    assert tm is None or not tm.servable
    if tm is None:
        assert diag.reason


# --- local tempo path: drifting drummers and mid-song tempo changes ---------

def _drifting_song(
    seconds: float = 60.0,
    bpm: float = 120.0,
    drift: float = 0.05,
    quiet_until: float | None = None,
    amp_quiet: float = 0.3,
) -> tuple[np.ndarray, np.ndarray]:
    """Kicks whose period breathes ±drift (a live drummer). Returns (sig, times)."""
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    kick = _kick(int(0.1 * _SR))
    times = []
    t = 0.30
    while t < seconds:
        times.append(t)
        i = int(t * _SR)
        amp = amp_quiet if (quiet_until is not None and t < quiet_until) else 1.0
        seg = kick[: len(sig) - i] * amp
        sig[i : i + len(seg)] += seg
        period = 60.0 / bpm * (1.0 + drift * np.sin(2 * np.pi * t / 20.0))
        t += period
    peak = np.max(np.abs(sig))
    return (sig / peak * 0.9 if peak else sig), np.array(times)


def test_drifting_tempo_is_tracked():
    # ±5% period modulation = a human drummer. The old global-period MAD gate
    # (0.12) rejected this class outright; the tempogram path must ride it.
    sig, true = _drifting_song(seconds=60.0, bpm=120.0, drift=0.05)
    tm = analyze_pcm(sig)
    assert tm is not None and tm.grid_usable, (
        f"drifting groove not usable: conf {0.0 if tm is None else tm.confidence:.2f}"
    )
    close = checked = 0
    for b in tm.beats:
        if b < 2.0 or b > 58.0:
            continue
        checked += 1
        if np.min(np.abs(true - b)) < 0.06:
            close += 1
    assert checked > 60 and close / checked >= 0.8


def test_mid_song_tempo_change_is_tracked():
    # 100 BPM verse into a 140 BPM chorus: one global tempo cannot fit both;
    # the Viterbi path must switch and the DP tracker must follow.
    seconds = 60.0
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    kick = _kick(int(0.1 * _SR))
    times = []
    t = 0.30
    while t < seconds:
        times.append(t)
        i = int(t * _SR)
        seg = kick[: len(sig) - i]
        sig[i : i + len(seg)] += seg
        t += 60.0 / (100.0 if t < 30.0 else 140.0)
    sig = sig / np.max(np.abs(sig)) * 0.9
    tm = analyze_pcm(sig)
    assert tm is not None and tm.grid_usable
    first = np.diff(tm.beats[(tm.beats > 4.0) & (tm.beats < 26.0)])
    second = np.diff(tm.beats[(tm.beats > 36.0) & (tm.beats < 56.0)])
    assert abs(np.median(first) - 0.600) < 0.03  # 100 BPM half
    assert abs(np.median(second) - 0.4286) < 0.03  # 140 BPM half


def test_dynamics_plus_drift_grid_usable():
    # The synthetic Hirudava: quiet verse (0.3 amp) + live-drum drift. On the
    # old analysis BOTH gates failed (diluted autocorrelation + wobbly-vs-
    # global-period MAD) and the song fell to the metadata animation.
    sig, _ = _drifting_song(
        seconds=60.0, bpm=110.0, drift=0.04, quiet_until=25.0, amp_quiet=0.3
    )
    tm = analyze_pcm(sig)
    assert tm is not None and tm.grid_usable, (
        f"Hirudava-class groove not usable: conf "
        f"{0.0 if tm is None else tm.confidence:.2f}"
    )


def test_tempogram_path_is_cheap():
    # The O() promise: tempogram + Viterbi on a 12-minute envelope must be
    # far below the decode time it rides on.
    from hue_music_sync.audio.trackmap import _tempo_path, _tempogram

    rng = np.random.default_rng(3)
    env = np.abs(rng.standard_normal(36_000))  # 12 min at 50 fps
    t0 = time.perf_counter()
    tg = _tempogram(env)
    assert tg is not None
    sal, lags, _centres, lag_refined = tg
    _path, _stab = _tempo_path(sal, lags, lag_refined)
    assert time.perf_counter() - t0 < 5.0  # generous CI bound; ~0.1 s locally
