"""Per-bulb instrument roles: assignment, rotation and role-true rendering."""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import (
    MODE_PARAMS,
    ROLE_BASS,
    ROLE_MID,
    ROLE_VOCAL,
    assign_roles,
)
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 3) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / max(1, n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


# --- assignment -----------------------------------------------------------

def test_assign_roles_high_mix_covers_all_three():
    roles = assign_roles(3, (0.4, 0.3, 0.3), 0)
    assert sorted(roles) == [ROLE_BASS, ROLE_MID, ROLE_VOCAL]


def test_assign_roles_intense_mix_two_to_one():
    roles = assign_roles(6, (0.67, 0.33, 0.0), 0)
    assert roles.count(ROLE_BASS) == 4
    assert roles.count(ROLE_MID) == 2
    assert ROLE_VOCAL not in roles


def test_assign_roles_all_bass_and_single_light():
    assert assign_roles(4, (1.0, 0.0, 0.0), 0) == [ROLE_BASS] * 4
    assert assign_roles(1, (0.4, 0.3, 0.3), 0) == [ROLE_BASS]  # bass wins a solo


def test_assign_roles_rotation_moves_the_band():
    base = assign_roles(3, (0.4, 0.3, 0.3), 0)
    rot = assign_roles(3, (0.4, 0.3, 0.3), 1)
    assert rot != base
    assert sorted(rot) == sorted(base)  # same band, different seats
    assert assign_roles(3, (0.4, 0.3, 0.3), 3) == base  # full circle


# The High split scaled cleanly for every light count a Hue entertainment area
# can hold (max 10). Bass keeps the plurality so the kick is always represented.
_HIGH_LADDER = {
    1: (1, 0, 0), 2: (1, 1, 0), 3: (1, 1, 1), 4: (2, 1, 1), 5: (2, 2, 1),
    6: (2, 2, 2), 7: (3, 2, 2), 8: (3, 3, 2), 9: (3, 3, 3), 10: (4, 3, 3),
}


def test_high_split_counts_scale_one_to_ten():
    for n, (nb, nm, nv) in _HIGH_LADDER.items():
        roles = assign_roles(n, (0.4, 0.3, 0.3), 0)
        assert len(roles) == n
        assert roles.count(ROLE_BASS) == nb, n
        assert roles.count(ROLE_MID) == nm, n
        assert roles.count(ROLE_VOCAL) == nv, n


def test_roles_are_spread_not_clustered():
    # Beating (bass) lights must be distributed around the room, never bunched.
    for n in (8, 10):
        roles = assign_roles(n, (0.4, 0.3, 0.3), 0)
        # No two adjacent lamps share a role at these counts (even interleave).
        assert all(roles[i] != roles[i + 1] for i in range(n - 1)), roles
    assert assign_roles(8, (0.4, 0.3, 0.3), 0) == [
        ROLE_BASS, ROLE_MID, ROLE_VOCAL, ROLE_BASS, ROLE_MID, ROLE_VOCAL,
        ROLE_BASS, ROLE_MID,
    ]
    assert assign_roles(10, (0.4, 0.3, 0.3), 0) == [
        ROLE_BASS, ROLE_MID, ROLE_VOCAL, ROLE_BASS, ROLE_MID, ROLE_VOCAL,
        ROLE_BASS, ROLE_MID, ROLE_VOCAL, ROLE_BASS,
    ]


def test_all_bass_mix_stays_all_bass_for_every_count():
    for n in range(1, 11):
        assert assign_roles(n, (1.0, 0.0, 0.0), 0) == [ROLE_BASS] * n


# --- rendering ------------------------------------------------------------

def _kick(strength: float = 2.5) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 1.0, "bass": 1.0, "low_mid": 0.2, "mid": 0.2, "high": 0.1},
        energy=0.9, beat=True, beat_strength=strength,
        bass_beat=True, bass_strength=strength,
    )


def _guitar_hit(strength: float = 2.2) -> AnalysisFrame:
    # A mid-band (guitar/snare) onset from the dedicated mid detector stream.
    return AnalysisFrame(
        bands={"sub_bass": 0.1, "bass": 0.1, "low_mid": 1.0, "mid": 1.0, "high": 0.4},
        energy=0.7, beat=True, beat_strength=strength,
        bass_beat=False, bass_strength=0.0,
        mid_beat=True, mid_strength=strength,
    )


def _quiet() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.1, "bass": 0.1, "low_mid": 0.1, "mid": 0.1, "high": 0.6},
        energy=0.3,
    )


def _bri(out, cid):
    return max(out[cid])


def _peak_per_cid(eng, frame, frames: int = 8) -> dict:
    """Per-channel peak brightness over a beat + its swell.

    Beats are slew-limited swings now (they brighten over a few frames rather
    than snapping in one), so the per-light effect is the peak across the window.
    """
    peak = {cid: max(c) for cid, c in eng.render(frame, _DT).items()}
    for _ in range(frames - 1):
        for cid, c in eng.render(_quiet(), _DT).items():
            peak[cid] = max(peak[cid], max(c))
    return peak


def _role_map(eng):
    return {cid: role for cid, role in eng.roles.items()}


def test_kick_snaps_bass_lights_not_vocal():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)
    eng.render(_quiet(), _DT)  # establish roles
    roles = _role_map(eng)
    peak = _peak_per_cid(eng, _kick())
    bass_cid = next(c for c, r in roles.items() if r == ROLE_BASS)
    vocal_cid = next(c for c, r in roles.items() if r == ROLE_VOCAL)
    assert peak[bass_cid] > peak[vocal_cid] + 0.25
    assert peak[bass_cid] > 0.6  # the kick genuinely snaps


def test_guitar_hit_pops_mid_light_only():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)
    eng.render(_quiet(), _DT)
    roles = _role_map(eng)
    out = eng.render(_guitar_hit(), _DT)
    mid_cid = next(c for c, r in roles.items() if r == ROLE_MID)
    bass_cid = next(c for c, r in roles.items() if r == ROLE_BASS)
    assert _bri(out, mid_cid) > _bri(out, bass_cid) + 0.2


def test_vocal_light_stays_dim_and_shimmers():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)
    eng.render(_quiet(), _DT)
    roles = _role_map(eng)
    vocal_cid = next(c for c, r in roles.items() if r == ROLE_VOCAL)
    levels = []
    for _ in range(100):
        out = eng.render(_quiet(), _DT)
        levels.append(_bri(out, vocal_cid))
    assert max(levels) < 0.6  # always a quiet layer
    assert max(levels) - min(levels) > 0.02  # but alive (shimmering)


def test_roles_rotate_after_the_scheduled_beats():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)
    eng.render(_quiet(), _DT)
    before = dict(eng.roles)
    rotate = MODE_PARAMS[SyncMode.HIGH].role_rotate_beats
    for _ in range(rotate):
        eng.render(_kick(), _DT)
        for _ in range(10):  # space the kicks past the onset refractory
            eng.render(_quiet(), _DT)
    after = dict(eng.roles)
    assert after != before
    assert sorted(after.values()) == sorted(before.values())


def test_extreme_sub_threshold_onset_stays_dimmer_than_a_big_kick():
    # Club Extreme reacts to far more now, but a genuinely sub-threshold onset
    # still must not flash like a real kick - the big kick slams much brighter.
    eng = EffectEngine(_channels(4))
    eng.set_mode(SyncMode.EXTREME)
    eng.render(_quiet(), _DT)
    small = _peak_per_cid(eng, _kick(strength=0.6))  # below the fire floor
    for _ in range(30):
        eng.render(_quiet(), _DT)  # let any response decay
    big = _peak_per_cid(eng, _kick(strength=2.8))
    assert max(big.values()) > max(small.values()) + 0.3
