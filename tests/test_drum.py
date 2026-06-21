"""Drum-pad manual beats: group split, manual flash, and auto-beat suppression."""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / max(1, n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _kick(strength: float = 2.5) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 1.0, "bass": 1.0, "low_mid": 0.2, "mid": 0.2, "high": 0.1},
        energy=0.9, beat=True, beat_strength=strength,
        bass_beat=True, bass_strength=strength,
    )


def _quiet() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.1, "bass": 0.1, "low_mid": 0.1, "mid": 0.1, "high": 0.1},
        energy=0.2,
    )


# --- pad -> light group split --------------------------------------------

def test_group_channels_splits_into_thirds():
    eng = EffectEngine(_channels(6))
    assert eng._group_channels("low") == eng._rank_ids[:2]
    assert eng._group_channels("mid") == eng._rank_ids[2:4]
    assert eng._group_channels("high") == eng._rank_ids[4:]
    assert eng._group_channels("all") == list(eng._rank_ids)


def test_group_channels_three_lights_are_one_each():
    eng = EffectEngine(_channels(3))
    assert eng._group_channels("low") == [eng._rank_ids[0]]
    assert eng._group_channels("mid") == [eng._rank_ids[1]]
    assert eng._group_channels("high") == [eng._rank_ids[2]]


def test_group_channels_few_lights_map_to_all():
    eng = EffectEngine(_channels(2))
    for g in ("low", "mid", "high"):
        assert eng._group_channels(g) == list(eng._rank_ids)


# --- manual flash ---------------------------------------------------------

def test_manual_flash_lights_its_group_and_decays():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)
    eng.set_manual_only(True)
    eng.render(_quiet(), _DT)
    low, high = eng._rank_ids[0], eng._rank_ids[2]
    eng.manual_flash("low", 0.95)
    out = eng.render(_quiet(), _DT)
    bright = max(out[low])
    assert bright > max(out[high]) + 0.2  # the tapped group pops, the rest don't
    for _ in range(12):
        out = eng.render(_quiet(), _DT)
    assert max(out[low]) < bright - 0.1  # and it fades back down


# --- automatic beats suppressed in drum mode ------------------------------

def _peak_after_kick(manual_only: bool) -> float:
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)
    eng.render(_quiet(), _DT)  # settle
    eng.set_manual_only(manual_only)
    # The beat is a slew-limited swing now, peaking a few frames after the tick;
    # take the peak over the kick + swell window.
    peak = max(max(c) for c in eng.render(_kick(), _DT).values())
    for _ in range(7):
        out = eng.render(_quiet(), _DT)
        peak = max(peak, max(max(c) for c in out.values()))
    return peak


def test_manual_only_suppresses_the_automatic_kick_flash():
    # A kick slams the room normally, but in drum mode it must not — the user's
    # taps drive the beats. The continuous layer still lights the room, so we
    # compare peaks rather than expecting darkness.
    assert _peak_after_kick(False) > _peak_after_kick(True) + 0.25
