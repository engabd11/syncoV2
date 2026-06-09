"""Predictive beat grid: tempo lock, phase accuracy, and graceful unlock."""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.audio.tempo import TempoTracker
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE


def _click_track(bpm: float, seconds: float, sr: int = ANALYSIS_SAMPLE_RATE) -> np.ndarray:
    sig = np.zeros(int(sr * seconds), dtype=np.float32)
    period = 60.0 / bpm
    for bt in np.arange(0.25, seconds, period):
        i = int(bt * sr)
        env = np.exp(-np.arange(int(0.1 * sr)) / (0.03 * sr))
        seg = (np.sin(2 * np.pi * 60 * np.arange(len(env)) / sr) * env).astype(np.float32)
        sig[i : i + len(seg)] += seg[: len(sig) - i]
    peak = np.max(np.abs(sig))
    return (sig / peak * 0.9) if peak else sig


def _run(sig: np.ndarray):
    a = Analyzer()
    tt = TempoTracker(a.frame_period)
    grids = []
    for k in range(len(sig) // ANALYSIS_HOP):
        f = a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        grids.append((f, tt.update(f.t_audio, f.flux, f.beat, f.beat_strength)))
    return grids


def test_tempo_locks_across_the_common_range():
    for bpm in (90, 120, 140, 174):
        grids = _run(_click_track(bpm, 12.0))
        final = grids[-1][1]
        assert final.locked
        assert abs(final.bpm - bpm) / bpm < 0.06  # within ~6% (lag quantisation)


def test_phase_aligns_to_real_beats():
    # Once locked, the beat clock's phase should be near a beat boundary at the
    # moments the audio actually has a beat.
    grids = _run(_click_track(120, 14.0))
    period = grids[-1][1].period_s
    errs = []
    for f, g in grids[len(grids) // 2 :]:  # second half, after lock
        if f.beat and g.locked:
            # circular distance of phase to the nearest beat boundary (0 or 1)
            errs.append(min(g.phase, 1.0 - g.phase))
    assert errs, "expected detected beats after lock"
    assert np.mean(errs) < 0.2  # within 20% of a beat of the real onset
    assert 0.45 < period < 0.55  # 120 BPM -> 0.5 s


def test_next_beat_is_in_the_future_and_within_a_period():
    grids = _run(_click_track(128, 10.0))
    for f, g in grids[-50:]:
        if g.locked:
            assert 0.0 <= g.time_to_next_beat <= g.period_s + 1e-6
            assert g.next_beat_t >= f.t_audio


def test_noise_does_not_lock():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(ANALYSIS_SAMPLE_RATE * 8) * 0.2).astype(np.float32)
    final = _run(noise)[-1][1]
    assert not final.locked  # irregular onsets -> no confident tempo


def test_tracks_a_tempo_change():
    sig = np.concatenate([_click_track(100, 8.0), _click_track(150, 8.0)])
    grids = _run(sig)
    # By the end it should have re-locked near the new tempo, not the old one.
    final = grids[-1][1]
    assert final.locked
    assert abs(final.bpm - 150) < abs(final.bpm - 100)
