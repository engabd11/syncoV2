"""The LedFx-style continuous foundation: the room is alive from melbank power.

The core regression guard for the "intense/extreme feel dead" complaint. The
visible show must NOT depend on a locked beat grid: with the grid forced
unlocked and *no detected beats at all*, the continuous melbank layer must keep
every lamp moving with the music. Beats only ever add punch on top.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, MELBANK_BINS, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_DT = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE


def _channels(n: int = 6) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=(i % 3) / 2.0)
        for i in range(n)
    ]


def _music(seconds: float = 12.0, bpm: float = 120.0, level: float = 0.9) -> np.ndarray:
    """A kick four-to-the-floor plus a fluctuating mid/high bed."""
    n = int(_SR * seconds)
    t = np.arange(n) / _SR
    rng = np.random.default_rng(3)
    bed = (rng.standard_normal(n).astype(np.float32) * 0.15
           * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t)).astype(np.float32))
    sig = bed
    env = np.exp(-np.arange(int(0.12 * _SR)) / (0.035 * _SR))
    kick = (np.sin(2 * np.pi * 55 * np.arange(len(env)) / _SR) * env).astype(np.float32)
    for bt in np.arange(0.3, seconds, 60.0 / bpm):
        i = int(bt * _SR)
        seg = kick[: n - i]
        sig[i : i + len(seg)] += seg
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak * level).astype(np.float32)


def _frames(sig: np.ndarray):
    a = Analyzer()
    return [a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
            for k in range(len(sig) // ANALYSIS_HOP)]


def _render_all(frames, mode: SyncMode):
    """Render every frame with NO beat grid (the grid-unlocked worst case)."""
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    room = []
    for f in frames:
        out = eng.render(f, _DT, beatgrid=None)
        room.append(max(max(c) for c in out.values()))
    return np.array(room)


# --- melbank normalization ---------------------------------------------------

def test_melbank_shape_and_normalization():
    a = Analyzer()
    # Loud broadband noise: bins should drive up toward full scale.
    rng = np.random.default_rng(1)
    loud = (rng.standard_normal(_SR * 2).astype(np.float32))
    loud /= np.max(np.abs(loud))
    mel = None
    for k in range(len(loud) // ANALYSIS_HOP):
        mel = a.push(loud[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]).melbank
    assert len(mel) == MELBANK_BINS
    assert max(mel) > 0.7  # gain-normalised toward 1 on loud input

    # Silence: the melbank decays back toward rest.
    silence = np.zeros(ANALYSIS_HOP, dtype=np.float32)
    for _ in range(200):
        mel = a.push(silence).melbank
    assert max(mel) < 0.05


# --- the core invariant: alive without any beats -----------------------------

def test_intense_is_alive_with_no_beatgrid_and_no_beats():
    # The whole point: feed real analyzed music but NO beat grid, so the engine
    # never sees a scheduled or detected beat drive the visible show. The
    # continuous melbank layer alone must keep the room lit and moving.
    frames = _frames(_music())
    room = _render_all(frames[30:], SyncMode.INTENSE)
    assert room.mean() > 0.30           # substantially lit, not dead
    assert float((room < 0.12).mean()) < 0.15  # rarely dark while playing
    assert room.std() > 0.02            # and it actually moves with the music


def test_extreme_is_alive_with_no_beatgrid():
    frames = _frames(_music())
    room = _render_all(frames[30:], SyncMode.EXTREME)
    assert room.mean() > 0.25
    assert room.std() > 0.02


# --- continuous layer tracks loudness ----------------------------------------

def test_continuous_layer_brighter_on_louder_music():
    loud = _render_all(_frames(_music(level=0.9))[30:], SyncMode.INTENSE)
    quiet = _render_all(_frames(_music(level=0.25))[30:], SyncMode.INTENSE)
    # The AGC normalises level over time, but the quieter track sits below the
    # noise gate more often, so its continuous layer reads dimmer on average.
    assert loud.mean() >= quiet.mean()


# --- reactivity increases up the ladder --------------------------------------

def test_higher_modes_are_more_reactive_than_subtle():
    frames = _frames(_music())[30:]
    subtle = _render_all(frames, SyncMode.SUBTLE)
    extreme = _render_all(frames, SyncMode.EXTREME)
    subtle_move = float(np.mean(np.abs(np.diff(subtle))))
    extreme_move = float(np.mean(np.abs(np.diff(extreme))))
    assert extreme_move > subtle_move + 0.01  # extreme swings, subtle is steady
