"""Pure-logic tests for the analyzer, encoder, palette and album-art DSP."""

from __future__ import annotations

import colorsys

import numpy as np
import pytest

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.color.album_art import _kmeans_palette
from hue_music_sync.color.palette import Palette, get_palette
from hue_music_sync.const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    ColorScheme,
)
from hue_music_sync.hue.stream import HueStreamEncoder, float_to_16, rgb8_to_16

_UUID = "abcdefab-1234-1234-1234-0123456789ab"


# --- encoder -------------------------------------------------------------

def test_scaling_endpoints():
    assert rgb8_to_16(0) == 0
    assert rgb8_to_16(255) == 65535
    assert float_to_16(0.0) == 0
    assert float_to_16(1.0) == 65535


def test_frame_length_and_header():
    enc = HueStreamEncoder(_UUID)
    frame = enc.build_frame_rgb({0: (1.0, 0.0, 0.0), 1: (0.0, 1.0, 0.0)})
    assert len(frame) == 52 + 2 * 7  # header + 2 channels
    assert frame[:9] == b"HueStream"
    assert frame[9:11] == b"\x02\x00"
    assert frame[16:52].decode() == _UUID
    # First channel: id 0, red full, green/blue zero.
    assert frame[52] == 0
    assert frame[53:55] == b"\xff\xff"
    assert frame[55:57] == b"\x00\x00"


def test_config_id_padding_does_not_crash():
    enc = HueStreamEncoder("short-id")
    frame = enc.build_frame_rgb({0: (0.0, 0.0, 0.0)})
    assert len(frame[16:52]) == 36  # padded to fixed width


# --- palette -------------------------------------------------------------

def test_palette_sample_is_cyclic():
    pal = Palette([(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)])
    assert pal.sample(0.0) == pytest.approx((1.0, 0.0, 0.0))
    assert pal.sample(1.0) == pytest.approx((1.0, 0.0, 0.0))  # wraps


def test_palette_spread_count():
    pal = get_palette(ColorScheme.PARTY)
    assert len(pal.spread(7)) == 7
    assert len(pal.spread(1)) == 1
    assert pal.spread(0) == []


def test_album_art_fallback_returns_palette():
    # ALBUM_ART has no static palette; it should fall back, not raise.
    assert len(get_palette(ColorScheme.ALBUM_ART).colors) >= 1


# --- album-art k-means ---------------------------------------------------

def test_kmeans_recovers_distinct_hues():
    # Equal blocks of red, green, blue.
    red = np.tile([1.0, 0.0, 0.0], (100, 1))
    green = np.tile([0.0, 1.0, 0.0], (100, 1))
    blue = np.tile([0.0, 0.0, 1.0], (100, 1))
    pixels = np.vstack([red, green, blue]).astype(np.float32)
    palette = _kmeans_palette(pixels, k=3)
    hues = sorted(round(colorsys.rgb_to_hsv(*c)[0] * 360) for c in palette[:3])
    # Expect hues near 0/120/240.
    assert any(h <= 10 or h >= 350 for h in hues)
    assert any(100 <= h <= 140 for h in hues)
    assert any(220 <= h <= 260 for h in hues)


# --- analyzer ------------------------------------------------------------

def _click_track(bpm: float, seconds: float) -> np.ndarray:
    sr = ANALYSIS_SAMPLE_RATE
    sig = np.zeros(int(sr * seconds), dtype=np.float32)
    period = 60.0 / bpm
    for bt in np.arange(0.25, seconds, period):
        i = int(bt * sr)
        env = np.exp(-np.arange(int(0.1 * sr)) / (0.03 * sr))
        seg = (np.sin(2 * np.pi * 60 * np.arange(len(env)) / sr) * env).astype(np.float32)
        sig[i : i + len(seg)] += seg[: len(sig) - i]
    peak = np.max(np.abs(sig))
    return (sig / peak * 0.9) if peak else sig


def test_analyzer_detects_beats_and_bass():
    sig = _click_track(120, 6.0)
    a = Analyzer()
    beats = 0
    max_bass = 0.0
    frame = None
    for k in range(len(sig) // ANALYSIS_HOP):
        frame = a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        beats += int(frame.beat)
        max_bass = max(max_bass, frame.bands["bass"])
    assert beats >= 8  # ~12 kicks in 6s, allow detector slack
    assert max_bass > 0.5  # 60 Hz energy lands in the bass band
    assert frame.bands["high"] < max_bass  # no treble content


def test_analyzer_silence_no_beats():
    a = Analyzer()
    silence = np.zeros(ANALYSIS_HOP, dtype=np.float32)
    beats = sum(a.push(silence).beat for _ in range(200))
    assert beats == 0
