"""The apartment-sync look (measured from the reference recording).

At high intensity the room is a single unified colour that JUMPS across the
spectrum on every beat, brightness slams bright only on the standout beats and
falls back to dark between them (~37% dark in the reference), and the lamps all
show the same hue. High keeps the older per-instrument spatial split instead.
"""

from __future__ import annotations

import colorsys

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.color.palette import get_palette
from hue_music_sync.const import ColorScheme, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import ROLE_BASS, ROLE_VOCAL
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / max(1, n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _quiet() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.2, "bass": 0.2, "low_mid": 0.2, "mid": 0.2, "high": 0.1},
        energy=0.4,
    )


def _grid(predicted: bool, phase: float = 0.3, accent: float = 0.8,
          beat_in_bar: int = 0) -> BeatGrid:
    return BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5, phase=phase,
        time_to_next_beat=(1.0 - phase) * 0.5, next_beat_t=0.0,
        bar_phase=(beat_in_bar + phase) / 4.0, predicted_beat=predicted,
        accent=accent, accent_now=accent, beat_in_bar=beat_in_bar,
    )


# A "dynamic mix": one standout per bar, one medium, two ordinary.
_PATTERN = (1.0, 0.3, 0.55, 0.3)


def _play_beat(eng: EffectEngine, accent: float, beat_in_bar: int):
    """One beat: a pre frame, the tick frame, then decay frames."""
    pre = eng.render(_quiet(), _DT, beatgrid=_grid(False, accent=accent,
                                                   beat_in_bar=beat_in_bar))
    tick = eng.render(_quiet(), _DT, beatgrid=_grid(True, accent=accent,
                                                    beat_in_bar=beat_in_bar))
    for _ in range(20):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, accent=accent,
                                                 beat_in_bar=beat_in_bar))
    return pre, tick


def _fill_window(eng: EffectEngine, bars: int = 6) -> None:
    for _ in range(bars):
        for b, acc in enumerate(_PATTERN):
            _play_beat(eng, acc, b)


def _room_hue_spread(out) -> float:
    """Max circular hue distance between any two lit lamps (0 = unified)."""
    hues = []
    for c in out.values():
        m = max(c)
        if m > 0.05:
            hues.append(colorsys.rgb_to_hsv(c[0] / m, c[1] / m, c[2] / m)[0])
    spread = 0.0
    for i in range(len(hues)):
        for j in range(i + 1, len(hues)):
            d = abs(hues[i] - hues[j])
            spread = max(spread, min(d, 1.0 - d))
    return spread


# --- selectivity: only standout beats flash --------------------------------

def test_extreme_fires_only_the_standout_beats():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    _fill_window(eng)

    # Ordinary off-downbeat (accent 0.3): the room stays dark.
    pre, tick = _play_beat(eng, 0.3, 1)
    for cid in pre:
        assert max(tick[cid]) < max(pre[cid]) + 0.05

    # Standout beat (accent 1.0, the bar's "one"): the room slams.
    pre, tick = _play_beat(eng, 1.0, 0)
    assert any(max(tick[c]) > max(pre[c]) + 0.3 for c in pre)


def test_extreme_alive_while_playing_dark_only_in_silence():
    # LedFx-style continuous foundation: while music plays the room stays ALIVE
    # between flashes (the continuous melbank layer keeps it lit) — it must NOT
    # collapse to black just because no beat fired. Only genuine silence rests
    # dark. This is the fix for "intense/extreme feel dead": the old design left
    # base/floor at 0, so a missed beat meant darkness.
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    _fill_window(eng)
    _play_beat(eng, 1.0, 0)  # a big flash
    out = None
    for _ in range(18):  # flash decays, no new beat, but music keeps playing
        out = eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.6))
    assert max(max(c) for c in out.values()) > 0.12  # still alive, not dead

    # Genuine silence (paused / between tracks): the continuous layer fades out
    # with the loudness gate and the room rests dark.
    silent = AnalysisFrame(
        bands={n: 0.0 for n in ("sub_bass", "bass", "low_mid", "mid", "high")},
        energy=0.0,
    )
    for _ in range(30):
        out = eng.render(silent, _DT, beatgrid=_grid(False, phase=0.6))
    assert max(max(c) for c in out.values()) < 0.08  # dark in silence


# --- unified room colour that jumps every beat -----------------------------

def test_extreme_room_is_one_unified_colour():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    eng.set_scheme(ColorScheme.RAINBOW)  # worst case for unity (full spectrum)
    out = None
    for _ in range(10):
        out = eng.render(_quiet(), _DT, beatgrid=_grid(False))
    assert _room_hue_spread(out) < 0.02  # every lamp the same hue


def test_extreme_colour_jumps_on_every_beat():
    # Colour is the primary motion: each beat advances the palette a lot more
    # than a beatless frame's slow drift.
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.EXTREME)
    for _ in range(8):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.5))
    before = eng.colour_phase
    eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.5))  # no beat
    drift = eng.colour_phase - before
    before = eng.colour_phase
    eng.render(_quiet(), _DT, beatgrid=_grid(True, accent=0.8))  # a beat
    jump = eng.colour_phase - before
    assert jump > drift + 0.10  # a real spectrum-spanning step on the beat


def test_high_keeps_the_spatial_role_spread():
    # High is the exception: it keeps distinct per-instrument hues across the
    # room rather than unifying.
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.HIGH)
    eng.set_scheme(ColorScheme.RAINBOW)
    out = None
    for _ in range(10):
        out = eng.render(_quiet(), _DT, beatgrid=_grid(False))
    assert _room_hue_spread(out) > 0.10  # lamps span a range of hues


# --- full-room moment (the role modes only) --------------------------------

def test_full_room_moment_takes_the_vocal_lights_in_high():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.HIGH)
    for _ in range(4):  # establish roles + warm the highlight window
        for b in range(4):
            _play_beat(eng, _PATTERN[b], b)
    vocal = [c for c, r in eng.roles.items() if r == ROLE_VOCAL]
    assert vocal  # High's role mix includes a vocal light on 5 channels

    # An ordinary highlight stays role-separated: vocal lights stay dim.
    pre, tick = _play_beat(eng, 0.85, 0)
    vocal = [c for c, r in eng.roles.items() if r == ROLE_VOCAL]
    assert all(max(tick[c]) < 0.5 for c in vocal)

    # A passage-topping hit (accent >= full_room_accent) slams every light.
    pre, tick = _play_beat(eng, 0.99, 0)
    vocal = [c for c, r in eng.roles.items() if r == ROLE_VOCAL]
    assert any(max(tick[c]) > max(pre[c]) + 0.25 for c in vocal)
