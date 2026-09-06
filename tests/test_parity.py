"""Behavioural parity small items (P6, from CAMusic).

- Warm-up highlight floor: a beat ranked before the accent window has context
  must still EARN its highlight.
- Idle show records slew memory and resets the pre-drop state machine, so
  resuming from idle never flashes the room.
- Per-metre bar weights: the pulse hierarchy keys off BeatGrid.beats_per_bar
  (3/4 waltz vs 4/4), with 4/4 unchanged.
"""

from __future__ import annotations

import pytest

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS, _bar_weights, pulse_weight
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 1.0 / 50.0


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _loud(beat: bool = False) -> AnalysisFrame:
    lvl = 1.0 if beat else 0.6
    return AnalysisFrame(
        bands={"sub_bass": lvl, "bass": lvl, "low_mid": lvl * 0.7, "mid": lvl * 0.5,
               "high": lvl * 0.4},
        energy=lvl,
        melbank=[0.4] * 16,
        salience=1.0,
    )


# --- warm-up highlight floor -------------------------------------------------

def test_warmup_highlight_must_be_earned():
    # With no ranking context yet, an accent at 0.2 must NOT qualify even
    # though the mode's accent_floor is 0 — the warm-up floor (0.3, from
    # CAMusic) stands in until the window has context. Once the window is
    # full of that same accent, ranking (not the floor) decides, and it does.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.HIGH)  # highlight_quantile=0.40, accent_floor=0.0
    p = eng.active_params
    assert p.accent_floor == 0.0
    assert not eng._beat_highlight(p, 0.2, append=True), (
        "the very first ordinary beat landed a full highlight"
    )
    for _ in range(10):
        eng._beat_highlight(p, 0.2, append=True)
    assert eng._beat_highlight(p, 0.2, append=False), (
        "a settled uniform passage should rank its own level as the pulse"
    )


def test_warmup_floor_does_not_block_genuine_hits():
    # A genuinely strong opening hit (accent well above the floor) still lands.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.HIGH)
    assert eng._beat_highlight(eng.active_params, 0.9, append=True)


# --- idle show slew memory + pre-drop reset ----------------------------------

def test_resuming_from_the_idle_show_does_not_flash_the_room():
    # ``_emit_b`` means "what this engine last put on the wire". The idle show
    # must rate-limit and RECORD its levels exactly like the music path, or the
    # first frame back on the music path clamps against a pre-pause value and
    # the room flashes to roughly that before sliding down.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.SUBTLE)

    # Play loudly enough to leave the emitted brightness high.
    for i in range(120):
        eng.render(_loud(beat=i % 20 == 0), _DT)

    # Pause: the idle show takes the room somewhere much dimmer.
    idle = 0.0
    for i in range(60):  # 6 s at 10 fps: clears even a slow fall cap
        out = eng.render_idle_show(float(i) * 0.1, 1.0, dt=0.1)
        idle = max(idle, max(max(c) for c in out.values()))

    # Resume on a quiet passage: the first frame continues from what is lit.
    first_back = max(max(c) for c in eng.render(AnalysisFrame(), _DT).values())
    assert first_back <= idle + 0.12, (
        f"resuming jumped from the idle level {idle} to {first_back}"
    )


def test_idle_show_resets_the_predrop_state_machine():
    # A held pre-drop must not survive a pause into the next track.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.INTENSE)
    eng._predrop = 0.8
    eng._predrop_commit = True
    eng._predrop_streak = 5
    eng.predrop_released = 0.5
    eng.render_idle_show(0.0, 1.0, dt=0.1)
    assert eng._predrop == 0.0
    assert eng._predrop_commit is False
    assert eng._predrop_streak == 0
    assert eng.predrop_released == 0.0


# --- per-metre bar weights ----------------------------------------------------

def test_bar_weights_per_metre():
    # 4/4 keeps its tuned hierarchy; 3/4 lands the "one" and carries the two
    # after it evenly. Out-of-range beats wrap by the metre's own length.
    assert _bar_weights(4) == (1.0, 0.72, 0.86, 0.72)
    assert _bar_weights(3) == (1.0, 0.72, 0.72)
    assert _bar_weights(5) == _bar_weights(4)  # unknown metre: common time


def test_pulse_weight_accepts_beats_per_bar():
    p = MODE_PARAMS[SyncMode.INTENSE]
    # beat 2 of a waltz carries 0.72; the same beat_in_bar under 4/4 would
    # carry 0.86 (beat 3's weight).
    w3 = pulse_weight(p, 0.8, 2, highlight=True, beats_per_bar=3)
    w4 = pulse_weight(p, 0.8, 2, highlight=True, beats_per_bar=4)
    assert w3 < w4
    # the default is 4/4 — every existing caller is unchanged
    assert pulse_weight(p, 0.8, 2, highlight=True) == w4


def test_beatgrid_defaults_to_common_time():
    g = BeatGrid()
    assert g.beats_per_bar == 4
    g3 = BeatGrid(bpm=150.0, beats_per_bar=3)
    assert g3.beats_per_bar == 3
