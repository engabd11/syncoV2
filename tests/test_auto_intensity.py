"""Tempo-driven Auto intensity: BPM -> Subtle/Medium/High with hysteresis.

Pure mapping (no Home Assistant), so the band boundaries and the anti-flicker
dead-zone are unit-tested directly.
"""

from __future__ import annotations

from hue_music_sync.const import (
    AUTO_BPM_HIGH,
    AUTO_BPM_LOW,
    AUTO_BPM_MARGIN,
    SyncMode,
)
from hue_music_sync.effects.modes import auto_mode_for_bpm


def test_bands_pick_expected_level():
    # Deep inside each band, the current level doesn't matter.
    assert auto_mode_for_bpm(80.0, SyncMode.MEDIUM) is SyncMode.SUBTLE
    assert auto_mode_for_bpm(110.0, SyncMode.SUBTLE) is SyncMode.MEDIUM
    assert auto_mode_for_bpm(150.0, SyncMode.MEDIUM) is SyncMode.HIGH


def test_never_auto_selects_manual_only_levels():
    for bpm in range(50, 220, 5):
        for current in SyncMode:
            assert auto_mode_for_bpm(float(bpm), current) in (
                SyncMode.SUBTLE,
                SyncMode.MEDIUM,
                SyncMode.HIGH,
            )


def test_hysteresis_low_boundary_is_sticky():
    lo, m = AUTO_BPM_LOW, AUTO_BPM_MARGIN
    just_below = lo - 1.0  # e.g. 94 with lo=95
    # Already Subtle: a BPM just under the boundary holds Subtle...
    assert auto_mode_for_bpm(just_below, SyncMode.SUBTLE) is SyncMode.SUBTLE
    # ...and even a hair above the raw boundary stays Subtle inside the dead-zone.
    assert auto_mode_for_bpm(lo + m - 0.5, SyncMode.SUBTLE) is SyncMode.SUBTLE
    # Only past the far edge does it step up to Medium.
    assert auto_mode_for_bpm(lo + m + 0.5, SyncMode.SUBTLE) is SyncMode.MEDIUM
    # Coming from Medium, it only drops to Subtle below the near edge.
    assert auto_mode_for_bpm(lo - m + 0.5, SyncMode.MEDIUM) is SyncMode.MEDIUM
    assert auto_mode_for_bpm(lo - m - 0.5, SyncMode.MEDIUM) is SyncMode.SUBTLE


def test_hysteresis_high_boundary_is_sticky():
    hi, m = AUTO_BPM_HIGH, AUTO_BPM_MARGIN
    # Already High: holds High down to hi - m.
    assert auto_mode_for_bpm(hi - m + 0.5, SyncMode.HIGH) is SyncMode.HIGH
    assert auto_mode_for_bpm(hi - m - 0.5, SyncMode.HIGH) is SyncMode.MEDIUM
    # Coming from Medium: only climbs into High past hi + m.
    assert auto_mode_for_bpm(hi + m - 0.5, SyncMode.MEDIUM) is SyncMode.MEDIUM
    assert auto_mode_for_bpm(hi + m + 0.5, SyncMode.MEDIUM) is SyncMode.HIGH


def test_no_oscillation_when_parked_on_a_boundary():
    # A track sitting exactly on the raw low boundary must not flip levels: with
    # the previous level fed back in each call, the result is a fixed point.
    bpm = AUTO_BPM_LOW
    level = SyncMode.MEDIUM
    seen = set()
    for _ in range(5):
        level = auto_mode_for_bpm(bpm, level)
        seen.add(level)
    assert len(seen) == 1  # settled, never bouncing between two bands
