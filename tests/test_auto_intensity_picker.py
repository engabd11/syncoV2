"""Musical Auto-intensity picker: live features -> a rung, gated to an enabled
set. Pure logic (no Home Assistant), so the mapping, the enabled-set clamp and
the anti-flicker behaviour are unit-tested directly.
"""

from __future__ import annotations

from hue_music_sync.const import DEFAULT_AUTO_LEVELS, SyncMode
from hue_music_sync.effects.modes import (
    AutoIntensityPicker,
    sanitize_auto_levels,
)

DEFAULT = tuple(DEFAULT_AUTO_LEVELS)                       # Subtle/Medium/High
WITH_INTENSE = DEFAULT + (SyncMode.INTENSE,)
ALL_RUNGS = WITH_INTENSE + (SyncMode.EXTREME,)


def _run(
    picker: AutoIntensityPicker,
    allowed: tuple[SyncMode, ...],
    *,
    energy: float,
    salience: float,
    bpm: float,
    beat_period_s: float = 0.4,
    seconds: float = 6.0,
    dt: float = 0.02,
) -> SyncMode:
    """Drive the picker with a steady synthetic feature stream; return the
    settled rung. Long enough to clear the dwell and the smoothing."""
    t = 0.0
    last_beat = -1e9
    level = None
    for _ in range(int(seconds / dt)):
        beat = (t - last_beat) >= beat_period_s
        if beat:
            last_beat = t
        level = picker.update(
            dt, energy=energy, salience=salience, bpm=bpm, beat=beat, allowed=allowed
        )
        t += dt
    assert level is not None
    return level


# --- the enabled-set clamp is the headline behaviour ------------------------

def test_default_set_caps_at_high_on_a_huge_moment():
    # A maximal moment would map to Extreme, but the default set stops at High.
    level = _run(
        AutoIntensityPicker(), DEFAULT,
        energy=1.0, salience=1.0, bpm=160, beat_period_s=0.25,
    )
    assert level is SyncMode.HIGH


def test_intense_reached_only_when_enabled():
    kw = dict(energy=0.95, salience=0.9, bpm=135, beat_period_s=0.4)
    assert _run(AutoIntensityPicker(), DEFAULT, **kw) is SyncMode.HIGH
    assert _run(AutoIntensityPicker(), WITH_INTENSE, **kw) is SyncMode.INTENSE


def test_extreme_reached_only_on_a_maximal_moment_and_when_enabled():
    kw = dict(energy=1.0, salience=1.0, bpm=160, beat_period_s=0.25)
    # Capped to Intense when Extreme is not enabled...
    assert _run(AutoIntensityPicker(), WITH_INTENSE, **kw) is SyncMode.INTENSE
    # ...and only the full set can reach Extreme.
    assert _run(AutoIntensityPicker(), ALL_RUNGS, **kw) is SyncMode.EXTREME


def test_calm_music_sits_low():
    level = _run(
        AutoIntensityPicker(), ALL_RUNGS,
        energy=0.35, salience=0.3, bpm=95, beat_period_s=0.8,
    )
    assert level in (SyncMode.SUBTLE, SyncMode.MEDIUM)


def test_mid_energy_lands_mid_ladder():
    level = _run(
        AutoIntensityPicker(), ALL_RUNGS,
        energy=0.6, salience=0.45, bpm=110, beat_period_s=0.5,
    )
    assert level in (SyncMode.MEDIUM, SyncMode.HIGH)


def test_never_returns_a_rung_outside_the_enabled_set():
    # Even a maximal moment, and even a sparse/odd enabled set, stays inside it.
    for allowed in (DEFAULT, WITH_INTENSE, ALL_RUNGS, (SyncMode.HIGH,),
                    (SyncMode.SUBTLE, SyncMode.INTENSE)):
        p = AutoIntensityPicker()
        for energy in (0.0, 0.5, 1.0):
            level = _run(p, allowed, energy=energy, salience=1.0, bpm=140)
            assert level in allowed


def test_single_enabled_rung_is_pinned():
    level = _run(
        AutoIntensityPicker(), (SyncMode.MEDIUM,),
        energy=1.0, salience=1.0, bpm=160, beat_period_s=0.25,
    )
    assert level is SyncMode.MEDIUM


# --- the selected set defines the operating range (the headline change) ------

def test_floor_is_the_lowest_enabled_rung():
    # With High as the lowest enabled rung, even a near-silent passage sits on
    # High — never below it.
    allowed = (SyncMode.HIGH, SyncMode.INTENSE, SyncMode.EXTREME)
    level = _run(
        AutoIntensityPicker(), allowed,
        energy=0.2, salience=0.1, bpm=90, beat_period_s=1.0,
    )
    assert level is SyncMode.HIGH


def test_top_enabled_rung_is_used_on_a_big_moment():
    # A full drop reaches the highest enabled rung (here Extreme), i.e. the top
    # of the range gets used rather than capping short.
    allowed = (SyncMode.MEDIUM, SyncMode.HIGH, SyncMode.INTENSE, SyncMode.EXTREME)
    level = _run(
        AutoIntensityPicker(), allowed,
        energy=1.0, salience=1.0, bpm=160, beat_period_s=0.25,
    )
    assert level is SyncMode.EXTREME


def test_range_spreads_across_a_sparse_selection():
    # A two-rung selection uses BOTH ends: quiet -> the low one, loud -> the high
    # one, even when they're far apart on the ladder.
    allowed = (SyncMode.MEDIUM, SyncMode.EXTREME)
    quiet = _run(AutoIntensityPicker(), allowed,
                 energy=0.3, salience=0.25, bpm=95, beat_period_s=0.9)
    loud = _run(AutoIntensityPicker(), allowed,
                energy=1.0, salience=1.0, bpm=160, beat_period_s=0.25)
    assert quiet is SyncMode.MEDIUM
    assert loud is SyncMode.EXTREME


# --- anti-flicker: dwell + hysteresis ---------------------------------------

def test_does_not_thrash_between_rungs():
    """A steady stream settles and holds — few switches, none at the end."""
    p = AutoIntensityPicker()
    t = 0.0
    last_beat = -1e9
    switches = 0
    prev = None
    tail = []
    for i in range(int(8.0 / 0.02)):
        beat = (t - last_beat) >= 0.4
        if beat:
            last_beat = t
        lvl = p.update(
            0.02, energy=0.9, salience=0.85, bpm=130, beat=beat, allowed=ALL_RUNGS
        )
        if prev is not None and lvl is not prev:
            switches += 1
        prev = lvl
        if i > int(6.0 / 0.02):
            tail.append(lvl)
        t += 0.02
    # It ramps up over a dwell or two, then holds rock-steady.
    assert switches <= 3
    assert len(set(tail)) == 1


def test_immediate_repick_applies_a_checklist_change_without_the_dwell():
    p = AutoIntensityPicker()
    kw = dict(energy=1.0, salience=1.0, bpm=160, beat=True)
    # Settle capped at High under the default set.
    for _ in range(300):
        p.update(0.02, allowed=DEFAULT, **kw)
    assert p.level is SyncMode.HIGH
    # Enabling Intense + clearing the dwell lets the very next frame climb.
    p.allow_immediate_repick()
    level = p.update(0.02, allowed=ALL_RUNGS, **kw)
    assert level in (SyncMode.INTENSE, SyncMode.EXTREME)


# --- sanitising the user-supplied enabled set -------------------------------

def test_sanitize_orders_and_dedupes():
    assert sanitize_auto_levels(["high", "subtle", "subtle"]) == (
        SyncMode.SUBTLE, SyncMode.HIGH,
    )


def test_sanitize_accepts_enum_members():
    assert sanitize_auto_levels([SyncMode.EXTREME, SyncMode.MEDIUM]) == (
        SyncMode.MEDIUM, SyncMode.EXTREME,
    )


def test_sanitize_drops_auto_and_unknown_and_falls_back_when_empty():
    assert sanitize_auto_levels(["auto", "bogus"]) == tuple(DEFAULT_AUTO_LEVELS)
    assert sanitize_auto_levels([]) == tuple(DEFAULT_AUTO_LEVELS)
    assert sanitize_auto_levels(None) == tuple(DEFAULT_AUTO_LEVELS)
