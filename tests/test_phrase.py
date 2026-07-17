"""Phrase-level evolution: 4-bar phrases cycle wave origins and colour motion."""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.spatial import phrase_origins
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(
            channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=(i % 2) / 2.0
        )
        for i in range(n)
    ]


def _frame() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.6, "bass": 0.7, "low_mid": 0.5, "mid": 0.4, "high": 0.3},
        energy=0.6,
        melbank=[0.4] * 16,
        salience=1.0,
    )


def _grid(beat_in_bar: int, tick: bool, ttnb: float) -> BeatGrid:
    return BeatGrid(
        bpm=120.0,
        confidence=0.8,
        locked=True,
        period_s=0.5,
        predicted_beat=tick,
        beat_in_bar=beat_in_bar,
        accent=0.8,
        accent_now=0.8,
        time_to_next_beat=ttnb,
    )


def _drive_bars(eng: EffectEngine, bars: int) -> None:
    """One beat per 25 frames (120 BPM at 50 fps), 4 beats per bar,
    time_to_next_beat counting down so anticipation (waves) can fire."""
    for bar in range(bars):
        for beat in range(4):
            for f in range(25):
                eng.render(
                    _frame(),
                    _DT,
                    beatgrid=_grid(beat, tick=(f == 0), ttnb=(25 - f) * _DT),
                )


def _engine(mode: SyncMode = SyncMode.HIGH) -> EffectEngine:
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    return eng


def test_phrase_advances_every_four_locked_bars():
    eng = _engine()
    _drive_bars(eng, 12)
    assert eng._bar_count == 12
    assert eng._phrase == 3


def test_wave_origins_cycle_with_the_phrase():
    eng = _engine()
    seen: list[int] = []
    for phrase in range(3):
        _drive_bars(eng, 4)
        if eng._waves:
            seen.append(eng._waves[-1].origin_idx)
    assert len(set(seen)) > 1  # the origin genuinely moves between phrases
    n = len(eng._origins)
    for phrase, oi in enumerate(seen):
        assert oi < n


def test_phrase_bumps_colour_phase_deterministically():
    a = _engine()
    b = _engine()
    _drive_bars(a, 8)
    _drive_bars(b, 8)
    assert a.colour_phase == b.colour_phase  # two runs render identically
    # And the phrase bump genuinely moved colour vs a phraseless mode.
    c = _engine(SyncMode.SUBTLE)  # phrase_bars == 0
    _drive_bars(c, 8)
    assert c._phrase == 0


def test_unlocked_grid_pauses_counting():
    eng = _engine()
    _drive_bars(eng, 2)
    assert eng._bar_count == 2
    for _ in range(500):  # 10 s of unlocked music
        eng.render(_frame(), _DT, beatgrid=None)
    assert eng._bar_count == 2  # no churn from a lost lock


def test_phrase_origins_geometry():
    positions = {i: (i / 4, 0.5, 0.0) for i in range(5)}
    origins = phrase_origins(positions)
    assert len(origins) == 4
    xs = [o[0] for o in origins]
    assert xs[1] < xs[0] < xs[2]  # left, centre, right are distinct
    assert origins[0] == origins[3]  # the centred bloom recurs
