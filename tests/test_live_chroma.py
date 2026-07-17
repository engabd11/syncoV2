"""Live chroma → Song colours on tracks with no offline map.

The analyzer folds each frame's FFT into 12 pitch classes; the coordinator
EMAs them (~7 s) and applies a palette via the same chroma→hue mapping the
offline sections use, throttled so colour only moves on real harmonic shifts.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.color.song_palette import (
    chroma_distance,
    should_update_palette,
)
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE

_SR = ANALYSIS_SAMPLE_RATE


def _frames_of(sig: np.ndarray):
    a = Analyzer()
    return [
        a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        for k in range(len(sig) // ANALYSIS_HOP)
    ]


# --- analyzer live chroma ------------------------------------------------------


def test_sine_440_lands_on_pitch_class_a():
    t = np.arange(_SR * 2) / _SR
    sig = (0.6 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    frames = _frames_of(sig)
    last = frames[-1]
    assert last.chroma, "chroma missing on a loud tonal frame"
    assert len(last.chroma) == 12
    assert int(np.argmax(last.chroma)) == 9  # A


def test_silence_leaves_chroma_empty():
    frames = _frames_of(np.zeros(_SR, dtype=np.float32))
    assert all(not f.chroma for f in frames)


def test_map_replay_frames_default_to_empty_chroma():
    # Frame producers that don't compute chroma (map replay, metadata) leave
    # the field empty, so the live-chroma path simply never engages for them.
    from hue_music_sync.audio.analyzer import AnalysisFrame

    assert AnalysisFrame().chroma == []


# --- throttle helper -----------------------------------------------------------


def _key(pc: int) -> list[float]:
    v = [0.05] * 12
    v[pc] = 1.0
    return v


def test_first_application_is_immediate():
    assert should_update_palette(None, _key(0), 0.0)


def test_hold_window_blocks_early_changes():
    assert not should_update_palette(_key(0), _key(6), 1.0)  # big shift, too soon


def test_big_harmonic_shift_applies_after_hold():
    assert should_update_palette(_key(0), _key(6), 4.0)


def test_same_key_never_churns():
    assert not should_update_palette(_key(0), _key(0), 60.0)


def test_small_drift_waits_for_refresh_window():
    drift = _key(0)
    drift[1] = 0.62  # a moderate secondary tone
    d = chroma_distance(_key(0), drift)
    assert 0.10 < d < 0.30  # genuinely "small drift" for this test
    assert not should_update_palette(_key(0), drift, 5.0)
    assert should_update_palette(_key(0), drift, 9.0)
