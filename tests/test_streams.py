"""The new onset streams (v1.23): treble ticks (hi-hats) and lead-note events,
plus the engine's melody-follow that rides the note stream in beatless passages.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer, AnalysisFrame
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_DT = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE


def _analyse(sig: np.ndarray) -> list[AnalysisFrame]:
    a = Analyzer()
    return [
        a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        for k in range(len(sig) // ANALYSIS_HOP)
    ]


def _bursts(carrier_hz: float, every_s: float, seconds: float = 6.0,
            decay_s: float = 0.03, amp: float = 0.6) -> np.ndarray:
    """Sharp-attack decaying bursts of a carrier — a synthetic hit stream."""
    n = int(_SR * seconds)
    sig = np.zeros(n, dtype=np.float32)
    env = np.exp(-np.arange(int(4 * decay_s * _SR)) / (decay_s * _SR))
    hit = (np.sin(2 * np.pi * carrier_hz * np.arange(len(env)) / _SR) * env)
    for bt in np.arange(0.3, seconds, every_s):
        i = int(bt * _SR)
        seg = hit[: n - i]
        sig[i : i + len(seg)] += seg.astype(np.float32)
    return sig * amp


def test_hihat_bursts_fire_the_high_stream_not_the_bass_stream():
    frames = _analyse(_bursts(6500.0, every_s=0.16))
    highs = sum(f.high_beat for f in frames)
    basses = sum(f.bass_beat for f in frames)
    assert highs >= 10  # the tick stream hears the hats
    assert basses == 0  # and they never leak into the kick stream


def test_kicks_do_not_fire_the_high_stream():
    frames = _analyse(_bursts(55.0, every_s=0.5, decay_s=0.05))
    assert sum(f.bass_beat for f in frames) >= 5
    assert sum(f.high_beat for f in frames) == 0


def test_lead_notes_fire_the_note_stream_and_kicks_do_not():
    # A melody line: 800 Hz note attacks with no bass under them.
    notes = _analyse(_bursts(800.0, every_s=0.4, decay_s=0.08))
    assert sum(f.note_beat for f in notes) >= 5
    # A kick is bass-dominant: it is a beat, never a "note".
    kicks = _analyse(_bursts(55.0, every_s=0.5, decay_s=0.05))
    assert sum(f.note_beat for f in kicks) == 0


# --- engine melody follow --------------------------------------------------------

def _channels(n: int = 4) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.5)
        for i in range(n)
    ]


def _note_frame(note: bool) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.05, "bass": 0.05, "low_mid": 0.4, "mid": 0.5, "high": 0.2},
        energy=0.35,
        melbank=[0.3] * 16,
        salience=0.7,
        note_beat=note,
        note_strength=2.0 if note else 0.0,
    )


def test_melody_follow_swells_on_notes_while_no_beat_is_discernible():
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.HIGH)
    for _ in range(30):
        eng.render(_note_frame(False), _DT)
    assert eng.melody_level < 0.05  # nothing without notes
    eng.render(_note_frame(True), _DT)
    lvl = eng.melody_level
    assert lvl > 0.3  # a lead note swells the room
    for _ in range(60):
        eng.render(_note_frame(False), _DT)
    assert eng.melody_level < lvl * 0.2  # and it breathes back down


def test_melody_follow_stands_down_when_the_groove_returns():
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.HIGH)
    eng._rhythm_conf = 1.0  # a locked groove is playing
    eng.render(_note_frame(True), _DT)
    assert eng.melody_level < 1e-6  # the beat owns the show; no melody layer
