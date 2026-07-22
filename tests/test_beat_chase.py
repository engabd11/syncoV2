"""Extreme's fast-beat spatial chase + instrument separation.

The user's complaint: Extreme dropped fast beats because the whole room flashed
as one unit and the field-safety limiter caps whole-field flashing. The fix
splits the instruments across lamps (drums vs guitar) and, when one instrument
runs fast, bounces its flash across ITS lamps (opposing sides alternating)
instead of pumping the whole room — so no beat is dropped and the flash reads as
movement. Slow beats still flash the whole role group (today's punch), and the
chase is Extreme-only (``beat_chase_hz`` is 0 everywhere else).
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import (
    ROLE_BASS,
    ROLE_MID,
    beat_group_count,
    chase_bucket,
)
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02  # 50 fps, matching the analysis hop


def _channels(n: int = 6) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _kick(strength: float = 2.5) -> AnalysisFrame:
    """A strong, broadband kick with NO mid/high content, so a bass-only track
    keeps every lamp on the drum role (no guitar lamp is reserved)."""
    return AnalysisFrame(
        bands={"sub_bass": 0.9, "bass": 0.9, "low_mid": 0.0, "mid": 0.0, "high": 0.0},
        energy=0.8, beat=True, beat_strength=strength,
        bass_beat=True, bass_strength=strength,
        melbank=[0.6, 0.6, 0.4, 0.2] + [0.0] * 12,
        salience=1.0, onset_width=0.6,
    )


def _bed() -> AnalysisFrame:
    """A loud-enough bass-only bed between kicks (keeps the silence gate open)."""
    return AnalysisFrame(
        bands={"sub_bass": 0.5, "bass": 0.5, "low_mid": 0.0, "mid": 0.0, "high": 0.0},
        energy=0.6, melbank=[0.4, 0.4, 0.3] + [0.0] * 13, salience=1.0, onset_width=0.6,
    )


def _full_band(bass: bool = False, mid: bool = False) -> AnalysisFrame:
    """Both bass and mid content present (so dynamic roles deal out mid lamps)."""
    return AnalysisFrame(
        bands={"sub_bass": 0.7, "bass": 0.7, "low_mid": 0.6, "mid": 0.6, "high": 0.3},
        energy=0.7, beat=bass or mid, beat_strength=2.2 if (bass or mid) else 0.0,
        bass_beat=bass, bass_strength=2.4 if bass else 0.0,
        mid_beat=mid, mid_strength=2.2 if mid else 0.0,
        melbank=[0.5] * 16, salience=1.0, onset_width=0.6,
    )


def _guitar() -> AnalysisFrame:
    """A pure guitar/snare (mid) onset — no kick."""
    return AnalysisFrame(
        bands={"sub_bass": 0.1, "bass": 0.1, "low_mid": 0.8, "mid": 0.8, "high": 0.4},
        energy=0.7, beat=True, beat_strength=2.2,
        mid_beat=True, mid_strength=2.2,
        melbank=[0.1, 0.1, 0.2, 0.4, 0.6, 0.6, 0.5, 0.4] + [0.3] * 8,
        salience=1.0, onset_width=0.6,
    )


def _flashed_up(eng: EffectEngine, prev: dict[int, float]) -> set[int]:
    """Lamps whose flash overlay ROSE this frame == the freshly-flashed set.

    The overlay only ever decays between beats, so any lamp that went up is one
    the beat just assigned — a clean read of exactly which lamps fired.
    """
    return {c for c, v in eng._light_flash.items() if v > prev.get(c, 0.0) + 0.05}


# --- the pure allocator ------------------------------------------------------

def test_beat_group_count_slow_or_off_is_one_group():
    assert beat_group_count(0.0, 6, 4.0) == 1     # no beats
    assert beat_group_count(3.0, 6, 4.0) == 1     # under target -> whole room
    assert beat_group_count(10.0, 6, 0.0) == 1    # chase disabled
    assert beat_group_count(10.0, 1, 4.0) == 1    # a single lamp can't split


def test_beat_group_count_scales_with_rate_and_clamps():
    assert beat_group_count(8.0, 6, 4.0) == 2
    assert beat_group_count(9.0, 6, 4.0) == 3
    assert beat_group_count(100.0, 6, 4.0) == 6   # never more groups than lamps


def test_chase_bucket_two_groups_is_a_clean_left_right():
    ids = [0, 1, 2, 3]
    assert chase_bucket(ids, 0, 2) == {0, 1}      # left
    assert chase_bucket(ids, 1, 2) == {2, 3}      # right
    assert chase_bucket(ids, 2, 2) == {0, 1}      # wraps


def test_chase_bucket_covers_every_lamp_over_a_cycle():
    ids = [10, 11, 12, 13, 14, 15]
    g = 3
    buckets = [chase_bucket(ids, c, g) for c in range(g)]
    assert set().union(*buckets) == set(ids)             # full coverage
    assert sum(len(b) for b in buckets) == len(ids)      # disjoint, contiguous
    assert all(buckets)                                   # no empty bucket
    assert min(buckets[0]) < min(buckets[1])             # alternating ends


def test_chase_bucket_one_group_is_the_whole_group():
    ids = [0, 1, 2, 3]
    assert chase_bucket(ids, 0, 1) == set(ids)
    assert chase_bucket(ids, 7, 1) == set(ids)


# --- the chase in the engine -------------------------------------------------

def test_slow_beats_flash_the_whole_room_in_extreme():
    # An isolated kick is not fast (rate ~0 -> 1 group) so the whole bass group
    # still fires together — today's dramatic whole-room punch is preserved.
    eng = EffectEngine(_channels(6))
    eng.set_mode(SyncMode.EXTREME)
    for _ in range(5):
        eng.render(_bed(), _DT)
    prev = dict(eng._light_flash)
    eng.render(_kick(), _DT)
    assert len(_flashed_up(eng, prev)) == 6  # all lamps (bass-only track)


def test_fast_kicks_chase_across_the_lamps():
    # A fast kick run must NOT flash the whole room every hit: it bounces across
    # the lamps, landing somewhere every beat (nothing dropped) and moving.
    eng = EffectEngine(_channels(6))
    eng.set_mode(SyncMode.EXTREME)
    sets: list[frozenset[int]] = []
    hits = 0
    for i in range(200):
        is_kick = i % 3 == 0  # ~16 kicks/s
        prev = dict(eng._light_flash)
        eng.render(_kick() if is_kick else _bed(), _DT)
        if is_kick:
            hits += 1
            if hits > 15:  # after the rate/confidence warm-up
                sets.append(frozenset(_flashed_up(eng, prev)))
        if len(sets) >= 8:
            break

    assert all(len(s) >= 1 for s in sets)     # every fast beat still lands
    assert all(len(s) < 6 for s in sets)      # never the whole room (separated)
    assert len(set(sets)) >= 3                # the flash genuinely moves around
    assert len(set().union(*sets)) >= 4       # spread across the room


def test_chase_is_extreme_only():
    # The same fast stream: Extreme separates, Intense keeps the whole-room punch
    # (beat_chase_hz is 0 outside Extreme, so nothing changed there).
    def run(mode: SyncMode) -> list[frozenset[int]]:
        eng = EffectEngine(_channels(6))
        eng.set_mode(mode)
        sets: list[frozenset[int]] = []
        hits = 0
        for i in range(160):
            is_kick = i % 3 == 0
            prev = dict(eng._light_flash)
            eng.render(_kick() if is_kick else _bed(), _DT)
            if is_kick:
                hits += 1
                if hits > 15:
                    sets.append(frozenset(_flashed_up(eng, prev)))
        return sets

    assert all(len(s) == 6 for s in run(SyncMode.INTENSE))  # whole room every hit
    assert any(len(s) < 6 for s in run(SyncMode.EXTREME))   # Extreme separates


def test_extreme_separates_drums_and_guitar_onto_different_lamps():
    # Instrument roles: after a full-band passage Extreme has both drum (bass)
    # and guitar (mid) lamps, and a kick lands on the drum lamps while a guitar
    # hit lands on the guitar lamps — never the same light.
    eng = EffectEngine(_channels(8))
    eng.set_mode(SyncMode.EXTREME)
    for i in range(500):
        eng.render(_full_band(bass=(i % 8 == 0), mid=(i % 8 == 4)), _DT)
    assert ROLE_BASS in eng.roles.values()
    assert ROLE_MID in eng.roles.values()

    prev = dict(eng._light_flash)
    eng.render(_kick(), _DT)
    roles = dict(eng.roles)  # roles are refreshed at the top of the frame
    fired = _flashed_up(eng, prev)
    assert fired and all(roles.get(c) == ROLE_BASS for c in fired)

    prev = dict(eng._light_flash)
    eng.render(_guitar(), _DT)
    roles = dict(eng.roles)
    fired = _flashed_up(eng, prev)
    assert fired and all(roles.get(c) == ROLE_MID for c in fired)


def test_fast_extreme_stays_lit_and_moving_through_the_relaxed_limiter():
    # The payoff: through the actual club-mode limiter, a fast (~8/s) kick run
    # keeps the room lit AND reads as movement across the lamps — beats are not
    # compressed away into darkness (which is what the whole-room flash did).
    from hue_music_sync.effects.safety import FieldSafety, RELAXED_MAX_FLASHES_PER_S

    eng = EffectEngine(_channels(6))
    eng.set_mode(SyncMode.EXTREME)
    safety = FieldSafety(max_flashes_per_s=RELAXED_MAX_FLASHES_PER_S, calm_gated=False)
    field_mean = []   # brightness averaged over the lamps each frame
    spatial_std = []  # spread across the lamps each frame (the chase pattern)
    for i in range(300):
        out = eng.render(_kick() if i % 6 == 0 else _bed(), _DT)  # ~8 kicks/s
        out = safety.process(out, _DT)
        vals = [max(c) for c in out.values()]
        field_mean.append(float(np.mean(vals)))
        spatial_std.append(float(np.std(vals)))
    fm, ss = np.array(field_mean[100:]), np.array(spatial_std[100:])
    assert fm.mean() > 0.25  # the room stays lit — the beat is never dropped to dark
    assert ss.mean() > 0.06  # the light moves ACROSS the lamps (a chase, not uniform)
