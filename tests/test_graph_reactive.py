"""Extreme, rebuilt as a direct 'the song is a graph' renderer.

Each lamp reflects its own slice of the spectrum: a glow proportional to that
band's loudness and a flash proportional to a fresh transient (peak) in it.
Instruments separate by frequency across the room, peaks flash in proportion to
their height, a sustained tone only glows (never strobes), and the beat grid is
ignored entirely — see EffectEngine._render_extreme.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import MELBANK_BINS, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 6) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _frame(melbank: list[float], energy: float = 0.5) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.4, "bass": 0.4, "low_mid": 0.4, "mid": 0.4, "high": 0.4},
        energy=energy, melbank=list(melbank),
    )


def _eng() -> EffectEngine:
    e = EffectEngine(_channels(6))
    e.set_mode(SyncMode.EXTREME)
    return e


def test_instruments_separate_by_frequency_across_the_room():
    # A low (bass) tone lights the LOW lamps (left) brighter than the high lamps;
    # a high (treble) tone does the opposite — instruments separate in space.
    low = [0.9] * 4 + [0.0] * (MELBANK_BINS - 4)
    high = [0.0] * (MELBANK_BINS - 4) + [0.9] * 4
    eng = _eng()
    for _ in range(40):
        out = eng.render(_frame(low), _DT)
    assert max(out[0]) > max(out[5]) + 0.15    # bass → low lamp brightest

    eng = _eng()
    for _ in range(40):
        out = eng.render(_frame(high), _DT)
    assert max(out[5]) > max(out[0]) + 0.15    # treble → high lamp brightest


def test_flash_scales_with_peak_height():
    # A bigger transient (peak) flashes brighter than a smaller one — reactions
    # are in direct proportion to how far the graph jumped up.
    def flash_for(jump: float) -> float:
        eng = _eng()
        base = [0.2] * MELBANK_BINS
        for _ in range(40):  # settle the per-bin baseline
            eng.render(_frame(base), _DT)
        peak = 0.0
        peaked = [0.2 + jump] * MELBANK_BINS
        for _ in range(6):  # the transient jump + its brief decay
            out = eng.render(_frame(peaked), _DT)
            peak = max(peak, max(max(c) for c in out.values()))
        return peak

    assert flash_for(0.7) > flash_for(0.2) + 0.15


def test_sustained_tone_glows_without_strobing():
    # A held tone (no fresh transient) gives a steady GLOW, not a strobe.
    eng = _eng()
    rooms = []
    for _ in range(120):
        out = eng.render(_frame([0.5] * MELBANK_BINS), _DT)
        rooms.append(max(max(c) for c in out.values()))
    r = np.array(rooms[40:])
    assert r.mean() > 0.15                 # a real lit glow (tracks loudness)
    assert float(np.std(np.diff(r))) < 0.02  # smooth — no strobing


def test_ignores_the_beat_grid_entirely():
    # A locked grid predicting beats must add NOTHING over a held tone: the graph
    # renderer never reads the grid, so there are no phantom/predicted beats.
    grid = BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5,
        predicted_beat=True, accent=1.0, accent_now=1.0,
    )
    mel = [0.4] * MELBANK_BINS
    eng = _eng()
    with_grid = []
    for i in range(60):
        out = eng.render(_frame(mel), _DT, beatgrid=grid)
        if i >= 40:
            with_grid.append(max(max(c) for c in out.values()))
    eng = _eng()
    no_grid = []
    for i in range(60):
        out = eng.render(_frame(mel), _DT)
        if i >= 40:
            no_grid.append(max(max(c) for c in out.values()))
    assert abs(np.mean(with_grid) - np.mean(no_grid)) < 1e-6


def test_fades_out_in_silence():
    eng = _eng()
    for _ in range(40):
        eng.render(_frame([0.5] * MELBANK_BINS), _DT)
    out = None
    for _ in range(60):  # audio stops
        out = eng.render(_frame([0.0] * MELBANK_BINS, energy=0.0), _DT)
    assert max(max(c) for c in out.values()) < 0.05  # rests dark
