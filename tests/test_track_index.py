"""Persistent per-track analysis index: records, budget, cross-mapper sharing."""

from __future__ import annotations

import asyncio
import time

import numpy as np

from hue_music_sync.audio.track_index import TrackIndex, track_key
from hue_music_sync.audio.trackmap import (
    MapResult,
    TrackFeatures,
    TrackMap,
    TrackMapper,
)


def _tiny_map() -> TrackMap:
    """A minimal grid-usable map, cheap enough to save repeatedly."""
    n = 2000  # 40 s of frames
    z = np.zeros(n, dtype=np.float32)
    feats = TrackFeatures(
        bands=np.zeros((n, 5), dtype=np.float32), energy=z + 0.5, flux=z,
        bass_flux=z, mid_flux=z, centroid=z + 0.5,
    )
    beats = np.arange(0.5, 40.0, 0.5)
    return TrackMap(
        duration=40.0, bpm=120.0, confidence=0.9, beats=beats,
        accents=np.full(beats.size, 0.5), downbeat=0, features=feats,
    )


def test_round_trip_persists_records(tmp_path):
    p = tmp_path / "track_index.json"
    idx = TrackIndex(p)
    idx.load_sync()
    idx.record(
        track_key("a|Artist|Song"),
        label="Artist - Song",
        status="failed",
        reason="HTTP 404",
        attempts=4,
        permanent=True,
    )
    idx.record(
        track_key("b|Artist|Ambient"),
        label="Artist - Ambient",
        status="ambient",
        reason="low grid confidence (0.21)",
        bpm=92.3,
        confidence=0.21,
    )
    idx.set_library_total(1234)
    asyncio.run(idx.flush(force=True))
    assert p.exists()

    fresh = TrackIndex(p)
    fresh.load_sync()
    rec = fresh.get(track_key("a|Artist|Song"))
    assert rec is not None
    assert rec["status"] == "failed" and rec["permanent"] and rec["attempts"] == 4
    assert rec["label"] == "Artist - Song" and rec["reason"] == "HTTP 404"
    amb = fresh.get(track_key("b|Artist|Ambient"))
    assert amb is not None and amb["status"] == "ambient"
    assert amb["bpm"] == 92.3 and amb["confidence"] == 0.21
    assert fresh.library_total == 1234


def test_flush_is_debounced_but_forceable(tmp_path):
    p = tmp_path / "track_index.json"
    idx = TrackIndex(p)
    idx.load_sync()
    idx.record(track_key("t1"), label="T1", status="failed", permanent=True)
    asyncio.run(idx.flush(force=True))
    first = p.stat().st_mtime_ns
    # A second dirty write immediately after is debounced away...
    idx.record(track_key("t2"), label="T2", status="failed", permanent=True)
    asyncio.run(idx.flush())
    assert p.stat().st_mtime_ns == first
    # ...but force writes it.
    asyncio.run(idx.flush(force=True))
    fresh = TrackIndex(p)
    fresh.load_sync()
    assert fresh.get(track_key("t2")) is not None


def test_clear_failures_keeps_ambient(tmp_path):
    idx = TrackIndex(tmp_path / "i.json")
    idx.load_sync()
    idx.record(track_key("f1"), label="F1", status="failed", permanent=True)
    idx.record(track_key("r1"), label="R1", status="retrying", attempts=2)
    idx.record(track_key("a1"), label="A1", status="ambient")
    assert idx.clear_failures() == 2
    assert idx.get(track_key("f1")) is None and idx.get(track_key("r1")) is None
    assert idx.ambient_keys() == [track_key("a1")]
    assert idx.counts() == {"failed": 0, "ambient": 1, "retrying": 0}


def test_failed_entries_capped_and_labelled(tmp_path):
    idx = TrackIndex(tmp_path / "i.json")
    idx.load_sync()
    for i in range(30):
        idx.record(
            track_key(f"t{i}"), label=f"Artist - Song {i}",
            status="failed", reason="x" * 300, permanent=True,
        )
    entries = idx.failed_entries(25)
    assert len(entries) == 25
    assert all(e["label"].startswith("Artist - Song") for e in entries)
    assert all(len(e["reason"]) <= 120 for e in entries)  # recorder-safe


def test_disk_budget_scales_with_library():
    idx = TrackIndex("unused.json")
    assert idx.disk_budget == 2048  # floor before any enumeration
    idx.set_library_total(4000)
    assert idx.disk_budget == 5000  # 1.25x headroom
    idx.set_library_total(100)
    assert idx.disk_budget == 2048  # never below the floor


def test_corrupt_index_file_is_tolerated(tmp_path):
    p = tmp_path / "track_index.json"
    p.write_text("{not json", encoding="utf-8")
    idx = TrackIndex(p)
    idx.load_sync()  # must not raise
    assert idx.get(track_key("anything")) is None
    assert idx.counts() == {"failed": 0, "ambient": 0, "retrying": 0}


def test_failure_verdict_is_shared_across_mappers(tmp_path):
    # The point of the index: the pre-warm's permanent verdict is visible to a
    # DIFFERENT mapper instance (the playback coordinator / a later session),
    # so the track is not silently re-decoded and re-failed at playback.
    idx = TrackIndex(tmp_path / "i.json")
    idx.load_sync()
    m1 = TrackMapper("ffmpeg", cache_dir=tmp_path, track_index=idx)
    tid = "lib://track/1|Artist|Song"
    m1._record_result(
        tid, "http://x", MapResult(None, decoded=True, complete=True),
        label="Artist - Song",
    )
    assert m1.failed(tid)

    m2 = TrackMapper("ffmpeg", cache_dir=tmp_path, track_index=idx)
    assert m2.failed(tid)  # no in-memory state of its own — the index answers

    # And it survives a "restart" (fresh index instance from the same file).
    asyncio.run(idx.flush(force=True))
    idx2 = TrackIndex(tmp_path / "i.json")
    idx2.load_sync()
    m3 = TrackMapper("ffmpeg", cache_dir=tmp_path, track_index=idx2)
    assert m3.failed(tid)
    # retry_failed's clear makes it analysable again.
    idx2.clear_failures()
    assert not m3.failed(tid)


def test_prewarm_skips_index_failed_tracks(tmp_path, monkeypatch):
    # A track the index already marks permanently failed must be skipped by a
    # fresh sweep (fast incremental re-runs), not re-decoded.
    import hue_music_sync.audio.trackmap as tmmod

    idx = TrackIndex(tmp_path / "i.json")
    idx.load_sync()
    tid = "t|bad|1"
    idx.record(track_key(tid), label="T - Bad", status="failed", permanent=True)

    calls: list[str] = []

    async def fake_build(ffmpeg, url, max_seconds=tmmod._MAX_TRACK_S):
        calls.append(url)
        return MapResult(None, decoded=False, error="should not run")

    monkeypatch.setattr(tmmod, "build_track_map", fake_build)
    mapper = TrackMapper("ffmpeg", cache_dir=tmp_path, track_index=idx)
    res = asyncio.run(mapper.prewarm([(tid, "http://lib/bad", "T - Bad")], delay_s=0))
    assert calls == []  # skipped without a decode
    assert (res.analysed, res.ambient, res.failed, res.considered) == (0, 0, 0, 1)


def test_mapper_prune_follows_index_budget(tmp_path):
    # With a small forced budget the prune trims; with the index's
    # library-sized budget it keeps everything the sweep writes.
    idx = TrackIndex(tmp_path / "i.json")
    idx.load_sync()
    idx.set_library_total(0)  # floor: 2048 — far above what we write
    mapper = TrackMapper("ffmpeg", cache_dir=tmp_path, track_index=idx)
    tm = _tiny_map()
    assert tm.grid_usable
    for i in range(5):
        mapper._save_disk(f"t|{i}", tm)
        time.sleep(0.01)  # distinct mtimes so eviction order is deterministic
    assert len(list(tmp_path.glob("*.npz"))) == 5
    small = TrackMapper("ffmpeg", cache_dir=tmp_path, max_disk=2)
    small._prune_disk()
    assert len(list(tmp_path.glob("*.npz"))) == 2
