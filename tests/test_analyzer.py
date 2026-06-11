"""SuperFlux onset quality: vibrato immunity and the bass-vs-treble split."""

from __future__ import annotations

import json
import struct

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.audio.snapcast import parse_server_settings
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE

_SR = ANALYSIS_SAMPLE_RATE


def _run(sig: np.ndarray):
    a = Analyzer()
    frames = []
    for k in range(len(sig) // ANALYSIS_HOP):
        frames.append(a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]))
    return frames


def _kick_track(bpm: float, seconds: float, freq: float = 60.0) -> np.ndarray:
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    period = 60.0 / bpm
    for bt in np.arange(0.25, seconds, period):
        i = int(bt * _SR)
        env = np.exp(-np.arange(int(0.1 * _SR)) / (0.03 * _SR))
        seg = (np.sin(2 * np.pi * freq * np.arange(len(env)) / _SR) * env).astype(np.float32)
        sig[i : i + len(seg)] += seg[: len(sig) - i]
    peak = np.max(np.abs(sig))
    return (sig / peak * 0.9) if peak else sig


def test_vibrato_does_not_register_as_beats():
    # A sustained 440 Hz tone with +-15 Hz vibrato at 6 Hz: constant loudness,
    # wobbling pitch. Plain spectral flux fires constantly on this; SuperFlux's
    # frequency max-filter must keep it near-silent in the onset stream.
    t = np.arange(int(_SR * 6.0)) / _SR
    phase = 2 * np.pi * (440.0 * t + (15.0 / 6.0) * np.sin(2 * np.pi * 6.0 * t) / (2 * np.pi))
    sig = (0.6 * np.sin(phase)).astype(np.float32)
    frames = _run(sig)
    beats = sum(f.beat for f in frames)
    bass_beats = sum(f.bass_beat for f in frames)
    assert beats <= 3  # the initial attack may count; sustained vibrato must not
    assert bass_beats <= 1


def test_bass_beats_fire_on_kicks_not_hihats():
    # Kicks (60 Hz thumps) on the grid plus off-grid "hi-hats" (5 kHz bursts):
    # the visible bass stream must follow the kicks and ignore the hats.
    seconds = 6.0
    sig = _kick_track(120, seconds, freq=60.0)
    # Add hat bursts halfway between kicks (the classic off-beat eighth).
    for bt in np.arange(0.50, seconds, 0.5):
        i = int(bt * _SR)
        env = np.exp(-np.arange(int(0.03 * _SR)) / (0.008 * _SR))
        hat = (0.5 * np.sin(2 * np.pi * 5000 * np.arange(len(env)) / _SR) * env)
        sig[i : i + len(hat)] += hat[: len(sig) - i].astype(np.float32)
    sig = sig / np.max(np.abs(sig)) * 0.9
    frames = _run(sig)

    kick_times = set(np.round(np.arange(0.25, seconds, 0.5) / 0.02).astype(int))
    on_kick = 0
    off_kick = 0
    for idx, f in enumerate(frames):
        if not f.bass_beat:
            continue
        # within +-3 frames (~60 ms) of a true kick?
        if any(abs(idx - kt) <= 3 for kt in kick_times):
            on_kick += 1
        else:
            off_kick += 1
    assert on_kick >= 6  # most kicks produce a bass beat
    assert off_kick <= 2  # hats almost never do


def test_pure_treble_bursts_do_not_drive_bass_stream():
    seconds = 5.0
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    for bt in np.arange(0.3, seconds, 0.4):
        i = int(bt * _SR)
        env = np.exp(-np.arange(int(0.04 * _SR)) / (0.01 * _SR))
        burst = (0.7 * np.sin(2 * np.pi * 6000 * np.arange(len(env)) / _SR) * env)
        sig[i : i + len(burst)] += burst[: len(sig) - i].astype(np.float32)
    frames = _run(sig)
    assert sum(f.beat for f in frames) >= 5  # broadband stream hears them (tempo)
    assert sum(f.bass_beat for f in frames) <= 1  # visible stream does not


# --- snapcast ServerSettings ----------------------------------------------

def test_parse_server_settings_extracts_buffer():
    body = json.dumps({"bufferMs": 1500, "latency": 0, "muted": False, "volume": 100}).encode()
    payload = struct.pack("<I", len(body)) + body
    settings = parse_server_settings(payload)
    assert settings is not None and settings["bufferMs"] == 1500


def test_parse_server_settings_garbage_is_none():
    assert parse_server_settings(b"\x02\x00\x00\x00{x") is None
    assert parse_server_settings(b"") is None
