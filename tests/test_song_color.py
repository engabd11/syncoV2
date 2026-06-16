"""Song-matched colour + precomputed melbank in the offline track map.

Covers the v1.11 "deep-sync" work: colours derived from the song's own harmony
(chroma -> hue), and the LedFx continuous melbank precomputed into the track map
so scheduled playback is as alive as the live tap.
"""

from __future__ import annotations

import colorsys

import numpy as np

from hue_music_sync.audio.trackmap import analyze_pcm
from hue_music_sync.color.song_palette import (
    PITCH_HUE,
    dominant_pitch_class,
    palette_from_chroma,
)
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, MELBANK_BINS, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE


def _hue_of(rgb) -> float:
    return colorsys.rgb_to_hsv(*rgb)[0]


def _circular_hue_dist(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


# --- the pitch-class -> hue mapping ------------------------------------------

def test_pitch_hue_table():
    assert len(PITCH_HUE) == 12
    assert PITCH_HUE[0] == 0.0  # C = red
    assert PITCH_HUE[9] == 9.0 / 12.0  # A


def test_palette_from_chroma_uses_dominant_pitch_hue():
    chroma = np.zeros(12)
    chroma[9] = 1.0  # pure A
    chroma[2] = 0.6  # plus some D
    pal = palette_from_chroma(chroma)
    assert pal is not None and len(pal.colors) >= 2
    hues = [_hue_of(c) for c in pal.colors]
    # The A pitch class (hue 0.75) must be represented.
    assert min(_circular_hue_dist(h, PITCH_HUE[9]) for h in hues) < 0.04


def test_palette_from_flat_or_empty_chroma_is_none():
    assert palette_from_chroma(np.zeros(12)) is None
    assert palette_from_chroma(np.ones(5)) is None  # wrong length


def test_dominant_pitch_class():
    c = np.zeros(12)
    c[7] = 1.0
    assert dominant_pitch_class(c) == 7
    assert dominant_pitch_class(np.zeros(12)) == -1


# --- offline analysis: melbank + section palettes ----------------------------

def _kick(n: int, freq: float = 45.0) -> np.ndarray:
    env = np.exp(-np.arange(n) / (0.035 * _SR))
    return (np.sin(2 * np.pi * freq * np.arange(n) / _SR) * env).astype(np.float32)


def _tonal_song(bpm: float = 120.0, seconds: float = 32.0, pitch_hz: float = 523.25) -> np.ndarray:
    """Kicks (sub-bass, excluded from chroma) + a sustained tonal pad."""
    n = int(_SR * seconds)
    t = np.arange(n) / _SR
    sig = (0.3 * np.sin(2 * np.pi * pitch_hz * t)).astype(np.float32)  # C5 pad
    kick = _kick(int(0.12 * _SR))
    for bt in np.arange(0.3, seconds, 60.0 / bpm):
        i = int(bt * _SR)
        seg = kick[: n - i]
        sig[i : i + len(seg)] += seg
    return (sig / np.max(np.abs(sig)) * 0.9).astype(np.float32)


def test_track_map_precomputes_melbank():
    tm = analyze_pcm(_tonal_song())
    assert tm is not None and tm.features is not None
    mel = tm.features.melbank
    assert mel.ndim == 2 and mel.shape[1] == MELBANK_BINS
    assert mel.shape[0] == tm.features.energy.shape[0]
    assert 0.0 <= float(mel.min()) and float(mel.max()) <= 1.0
    assert float(mel.max()) > 0.5  # gain-normalised toward 1

    # frame_at must hand the melbank to the engine via AnalysisFrame.
    frame = tm.frame_at(5.0, 4.98)
    assert frame is not None and len(frame.melbank) == MELBANK_BINS


def test_section_palette_reflects_the_song_key():
    tm = analyze_pcm(_tonal_song(pitch_hz=523.25))  # C5 -> pitch class C (hue 0)
    assert tm is not None and tm.sections
    palettes = [s.palette for s in tm.sections if s.palette]
    assert palettes  # at least one section got a harmonic palette
    hues = [_hue_of(c) for c in palettes[0]]
    # C (red, hue 0) is the dominant tone and must appear in the palette.
    assert min(_circular_hue_dist(h, 0.0) for h in hues) < 0.06


# --- scheduled playback is as alive as the live tap --------------------------

def _channels(n: int = 6):
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=(i % 3) / 2.0)
        for i in range(n)
    ]


def test_scheduled_playback_is_alive_via_precomputed_melbank():
    # The whole point of precomputing the melbank: a player driven by the track
    # map (no live audio) gets the continuous reactive layer too. Render the
    # map's frames through the engine with NO beat grid and confirm liveliness.
    tm = analyze_pcm(_tonal_song())
    assert tm is not None
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.INTENSE)
    period = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE
    room = []
    pos, prev = 6.0, None
    for _ in range(300):
        frame = tm.frame_at(pos, prev)
        if frame is None:
            break
        assert frame.melbank  # scheduled frames now carry the melbank
        out = eng.render(frame, period, beatgrid=None)
        room.append(max(max(c) for c in out.values()))
        prev = pos
        pos += period
    room = np.array(room)
    assert room.mean() > 0.30  # alive, not dead
    assert room.std() > 0.02   # and moving with the music


# --- tempo octave guard does not regress a clean tempo -----------------------

def test_clean_tempo_is_unchanged_by_octave_guard():
    tm = analyze_pcm(_tonal_song(bpm=120.0))
    assert tm is not None
    assert 116.0 <= tm.bpm <= 124.0  # still ~120, the guard left it alone
