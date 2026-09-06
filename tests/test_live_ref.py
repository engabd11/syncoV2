"""Live absolute melbank reference (P5, from CAMusic).

The published melbank is per-bin AGC'd, so it says WHEN a band is active but
not how loud it is in absolute terms. The analyzer now also publishes a slow
envelope of the RAW pre-AGC per-bin means, normalised by the loudest bin —
the offline scan's percentile, approximated live. Empty until it has settled
(a reference to the intro would mis-weight the rest of the song).
"""

from __future__ import annotations

import numpy as np
import pytest

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.const import ANALYSIS_HOP

_DT_HOP = ANALYSIS_HOP


def _tone(freq: float, seconds: float, sr: int = 22050) -> np.ndarray:
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _push_frames(a: Analyzer, audio: np.ndarray):
    return [a.push(audio[i : i + _DT_HOP]) for i in range(0, len(audio) - _DT_HOP, _DT_HOP)]


def test_live_ref_empty_until_settled():
    # A reference taken from the first second of a track is a reference to its
    # intro: nothing is published until ~8 s of music has been heard.
    a = Analyzer()
    audio = _tone(100.0, 3.0)
    frames = _push_frames(a, audio)
    assert frames, "test produced no frames"
    assert all(f.melbank_ref == [] for f in frames)


def test_live_ref_publishes_after_warmup():
    a = Analyzer()
    audio = _tone(100.0, 10.0)  # ~10 s: clears the 400-frame (~8 s) warm-up
    frames = _push_frames(a, audio)
    published = [f for f in frames if f.melbank_ref]
    assert published, "the live reference never settled"
    ref = published[-1].melbank_ref
    assert len(ref) == len(published[-1].melbank)
    assert all(0.0 < v <= 1.0 for v in ref)
    # peak bin is exactly 1 (it is the normaliser)
    assert max(ref) == pytest.approx(1.0)


def test_live_ref_orders_bands_by_absolute_loudness():
    # A bass-heavy tone must put the loudness where the bass bins are: the
    # shape the engine's per-bin weights expect.
    a = Analyzer()
    audio = _tone(80.0, 10.0)
    frames = _push_frames(a, audio)
    ref = [f.melbank_ref for f in frames if f.melbank_ref][-1]
    low = sum(ref[: len(ref) // 2])
    high = sum(ref[len(ref) // 2 :])
    assert low > high, "the live reference did not follow the spectrum's balance"


def test_live_ref_resets_between_tracks():
    a = Analyzer()
    _push_frames(a, _tone(100.0, 10.0))  # settled
    a.reset()
    frames = _push_frames(a, _tone(100.0, 1.0))
    assert all(f.melbank_ref == [] for f in frames), (
        "the new track inherited the previous track's reference"
    )
