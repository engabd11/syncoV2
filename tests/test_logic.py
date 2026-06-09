"""Pure-logic tests for the analyzer, encoder, palette and album-art DSP."""

from __future__ import annotations

import colorsys

import numpy as np
import pytest

from hue_music_sync.audio.analyzer import AnalysisFrame, Analyzer
from hue_music_sync.color.album_art import _kmeans_palette
from hue_music_sync.color.palette import Palette, get_palette
from hue_music_sync.const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    ColorScheme,
    SyncEffect,
    SyncMode,
)
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel
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


def test_xy_frame_carries_dedicated_brightness():
    # Default colourspace is xy+brightness; build() must select it.
    enc = HueStreamEncoder(_UUID)
    frame = enc.build({0: (0.0, 0.0, 1.0)})  # full-bright blue
    assert frame[14] == 0x01  # colourspace byte = xy+brightness
    # Channel 0: x, y, brightness (each uint16). Brightness for full value = max.
    x = int.from_bytes(frame[53:55], "big") / 65535
    y = int.from_bytes(frame[55:57], "big") / 65535
    bri = int.from_bytes(frame[57:59], "big") / 65535
    assert bri == 1.0  # dedicated brightness, not derived from shrinking RGB
    # Pure sRGB blue maps just outside Gamut C and is clamped to the blue vertex
    # (0.1532, 0.0475) — deterministic colour instead of the bridge guessing.
    assert abs(x - 0.1532) < 0.001 and abs(y - 0.0475) < 0.001


def test_xy_dimming_keeps_chromaticity_constant():
    enc = HueStreamEncoder(_UUID)
    full = enc.build({0: (0.0, 0.0, 1.0)})
    dim = enc.build({0: (0.0, 0.0, 0.2)})  # same hue, dimmer
    fx, fy = full[53:55], full[55:57]
    dx, dy = dim[53:55], dim[55:57]
    assert (fx, fy) == (dx, dy)  # chromaticity unchanged while dimming
    assert int.from_bytes(dim[57:59], "big") / 65535 == pytest.approx(0.2, abs=0.001)


# --- palette -------------------------------------------------------------

def test_palette_sample_is_cyclic():
    pal = Palette([(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)])
    assert pal.sample(0.0) == pytest.approx((1.0, 0.0, 0.0))
    assert pal.sample(1.0) == pytest.approx((1.0, 0.0, 0.0))  # wraps


def test_palette_spread_count():
    pal = get_palette(ColorScheme.SUNSET)
    assert len(pal.spread(7)) == 7
    assert len(pal.spread(1)) == 1
    assert pal.spread(0) == []


def test_album_art_fallback_returns_palette():
    # ALBUM_ART has no static palette; it should fall back, not raise.
    assert len(get_palette(ColorScheme.ALBUM_ART).colors) >= 1


def test_rainbow_scheme_spans_the_spectrum():
    pal = get_palette(ColorScheme.RAINBOW)
    hues = {round(colorsys.rgb_to_hsv(*c)[0] * 6) for c in pal.colors}
    assert len(hues) >= 5  # covers most of the hue wheel, not one colour family


# --- effect engine: colour shifts with the beat -------------------------

def _channels(n: int = 3) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _frame(beat: bool, strength: float = 0.0) -> AnalysisFrame:
    bands = {"sub_bass": 1.0, "bass": 1.0, "low_mid": 0.5, "mid": 0.3, "high": 0.2}
    return AnalysisFrame(bands=bands, energy=1.0, beat=beat, beat_strength=strength)


def test_colour_phase_advances_faster_with_beats():
    # With beats, the palette phase should move much more than pure time drift.
    drift = EffectEngine(_channels())
    drift.set_mode(SyncMode.HIGH)
    for _ in range(40):
        drift.render(_frame(beat=False), 0.025)

    beats = EffectEngine(_channels())
    beats.set_mode(SyncMode.HIGH)
    for i in range(40):
        beats.render(_frame(beat=(i % 5 == 0), strength=2.0), 0.025)

    assert beats.colour_phase > drift.colour_phase * 2


def test_subtle_mode_barely_steps_on_beats():
    # Subtle is mostly time-drift; its per-beat step is tiny by design.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.SUBTLE)
    for i in range(40):
        eng.render(_frame(beat=(i % 5 == 0), strength=2.0), 0.025)
    intense = EffectEngine(_channels())
    intense.set_mode(SyncMode.INTENSE)
    for i in range(40):
        intense.render(_frame(beat=(i % 5 == 0), strength=2.0), 0.025)
    assert intense.colour_phase > eng.colour_phase


# --- fireworks effect ----------------------------------------------------

def test_fireworks_ignites_on_beats_and_fades():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.INTENSE)
    eng.set_effect(SyncEffect.FIREWORKS)

    def brightness(out):
        return max(max(c) for c in out.values())

    # A big beat should light at least one channel brightly.
    out = eng.render(_frame(beat=True, strength=3.0), 0.025)
    assert brightness(out) > 0.5

    # With no further beats the burst should fade back toward the dim base.
    for _ in range(40):
        out = eng.render(_frame(beat=False), 0.025)
    assert brightness(out) < 0.3


def test_movie_mode_brightness_follows_loudness():
    # Movie mode brightness should track overall loudness, gently, not beats.
    def energy_frame(e: float) -> AnalysisFrame:
        return AnalysisFrame(
            bands={"sub_bass": 0.1, "bass": 0.1, "high": 0.05},
            energy=e, beat=False,
        )

    eng = EffectEngine(_channels())
    eng.set_effect(SyncEffect.MOVIES)
    for _ in range(150):  # let the slow easing settle on a loud passage
        out = eng.render(energy_frame(0.9), 0.025)
    loud = max(max(c) for c in out.values())
    for _ in range(250):  # then a long quiet passage
        out = eng.render(energy_frame(0.03), 0.025)
    quiet = max(max(c) for c in out.values())
    assert loud > quiet + 0.1  # louder scenes are clearly brighter


def test_movie_mode_does_not_flash_on_beats():
    # A strong beat must not produce a flash in Movies effect (calm, eye-friendly).
    eng = EffectEngine(_channels())
    eng.set_effect(SyncEffect.MOVIES)
    out = eng.render(
        AnalysisFrame(
            bands={"sub_bass": 1.0, "bass": 1.0}, energy=0.3,
            beat=True, beat_strength=3.0,
        ),
        0.025,
    )
    assert max(max(c) for c in out.values()) < 0.6  # no full-bright strobe pop


def test_fireworks_respects_master_brightness():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.INTENSE)
    eng.set_effect(SyncEffect.FIREWORKS)
    eng.set_brightness(0.5)
    out = eng.render(_frame(beat=True, strength=3.0), 0.025)
    assert max(max(c) for c in out.values()) <= 0.5 + 1e-6


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
