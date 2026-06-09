"""Eye-safety invariants: the non-bypassable flash limiter, red guard, gamut
clamp/slew and the analyzer noise gate.

These encode the pre-1.0 release checklist as executable guarantees: no effect
or intensity may flash the whole field more than 3×/second, the calm presets are
flash-free, saturated-red strobing desaturates, every colour stays in the bulb
gamut, and true silence rests.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.const import ANALYSIS_HOP, SyncEffect, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.safety import (
    FLASH_DELTA,
    MAX_FLASHES_PER_S,
    FieldSafety,
    _field_brightness,
    _field_redness,
)
from hue_music_sync.hue.bridge import EntertainmentChannel
from hue_music_sync.hue.stream import GAMUT_C, HueStreamEncoder, clamp_to_gamut, rgb_to_xy

_DT = 0.025  # 40 fps


# --- helpers -------------------------------------------------------------

def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _count_flashes(fields: list[float], delta: float = FLASH_DELTA) -> int:
    """Count WCAG-style flashes (opposing ≥delta transition pairs) in a series."""
    flashes = halves = direction = 0
    last_extreme = prev = fields[0]
    for f in fields[1:]:
        cur = 1 if f > prev + 1e-4 else -1 if f < prev - 1e-4 else direction
        if direction != 0 and cur != 0 and cur != direction:
            if abs(prev - last_extreme) >= delta:
                halves += 1
                if halves % 2 == 0:
                    flashes += 1
            last_extreme = prev
        if cur != 0:
            direction = cur
        prev = f
    return flashes


def _point_in_triangle(p, a, b, c) -> bool:
    def cross(o, q, r):
        return (q[0] - o[0]) * (r[1] - o[1]) - (q[1] - o[1]) * (r[0] - o[0])

    d1, d2, d3 = cross(a, b, p), cross(b, c, p), cross(c, a, p)
    neg = d1 < -1e-9 or d2 < -1e-9 or d3 < -1e-9
    pos = d1 > 1e-9 or d2 > 1e-9 or d3 > 1e-9
    return not (neg and pos)


# --- the headline guarantee: ≤3 full-field flashes / second --------------

@pytest.mark.parametrize("hold", [1, 2, 3, 4, 6])
def test_flash_limiter_bounds_any_strobe_rate(hold):
    # A pathological whole-field black<->white strobe at any rate must never
    # exceed the WCAG ceiling of 3 flashes in any rolling one-second window.
    safety = FieldSafety()
    max_window = 0
    for i in range(800):
        v = 1.0 if (i // hold) % 2 == 0 else 0.0
        safety.process({0: (v, v, v)}, _DT)
        max_window = max(max_window, len(safety._flashes))
    assert max_window <= MAX_FLASHES_PER_S


def test_flash_limiter_is_transparent_on_calm_content():
    # Swings below the flash threshold must pass through bit-for-bit (the limiter
    # only ever engages on genuine strobing; it never dulls normal motion).
    safety = FieldSafety()
    for i in range(400):
        v = 0.6 + 0.04 * math.sin(i * 0.4)  # ±4% swing, under the 10% threshold
        inp = {0: (v, v, v), 1: (v * 0.5, 0.0, v)}
        out = safety.process(inp, _DT)
        for cid in inp:
            assert out[cid] == pytest.approx(inp[cid], abs=1e-9)


@pytest.mark.parametrize(
    "mode",
    [SyncMode.SUBTLE, SyncMode.MEDIUM, SyncMode.HIGH, SyncMode.INTENSE, SyncMode.EXTREME],
)
def test_no_intensity_exceeds_the_flash_limit_on_aggressive_edm(mode):
    # Drive every intensity with relentless 200ms-spaced full-energy beats and
    # assert the emitted field never breaches the ceiling once safety is applied.
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    safety = FieldSafety()
    max_window = 0
    for i in range(800):
        out = eng.render(_aggressive(i), _DT)
        safety.process(out, _DT)
        max_window = max(max_window, len(safety._flashes))
    assert max_window <= MAX_FLASHES_PER_S


def test_fireworks_within_the_flash_limit():
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.INTENSE)
    eng.set_effect(SyncEffect.FIREWORKS)
    safety = FieldSafety()
    max_window = 0
    for i in range(800):
        out = eng.render(_aggressive(i), _DT)
        safety.process(out, _DT)
        max_window = max(max_window, len(safety._flashes))
    assert max_window <= MAX_FLASHES_PER_S


# --- the documented "calm guarantee": Subtle & Movie are flash-free ------

def _aggressive(i: int):
    from hue_music_sync.audio.analyzer import AnalysisFrame

    beat = i % 3 == 0  # a kick every 3 frames (~75ms) — worse than any real track
    lvl = 1.0 if beat else 0.15
    return AnalysisFrame(
        bands={"sub_bass": lvl, "bass": lvl, "low_mid": lvl * 0.7, "mid": lvl * 0.5, "high": lvl * 0.6},
        energy=lvl,
        beat=beat,
        beat_strength=3.0 if beat else 0.0,
    )


def test_subtle_is_flash_free_by_construction():
    # Subtle has no beat-flash overlay, so even before the safety stage an
    # aggressive track produces zero whole-field flashes.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.SUBTLE)
    fields = [_field_brightness(eng.render(_aggressive(i), _DT)) for i in range(800)]
    assert _count_flashes(fields) == 0


def test_movie_effect_is_flash_free_by_construction():
    eng = EffectEngine(_channels())
    eng.set_effect(SyncEffect.MOVIES)
    fields = [_field_brightness(eng.render(_aggressive(i), _DT)) for i in range(800)]
    assert _count_flashes(fields) == 0


# --- saturated-red guard -------------------------------------------------

def test_red_guard_desaturates_rapid_red_flashing():
    # A whole-field saturated-red strobe must come out desaturated (white mixed
    # in) on its lit frames, since red carries a stricter flash threshold. The
    # guard fires intermittently as the limiter pins and releases, so check that
    # it desaturates at least some lit frame.
    safety = FieldSafety()
    lit = []
    for i in range(200):
        v = 1.0 if i % 2 == 0 else 0.0
        out = safety.process({0: (v, 0.0, 0.0)}, _DT)[0]
        if i % 2 == 0:  # lit frame (not a floor-lifted dark frame)
            lit.append(out)
    assert any(min(g, b) > 0.01 for r, g, b in lit)  # white mixed into the red


def test_red_guard_leaves_non_red_palettes_alone():
    # A blue strobe is brightness-limited but never desaturated — the lit frames
    # stay pure blue (only the dark frames get a neutral floor lift).
    safety = FieldSafety()
    lit = []
    for i in range(200):
        v = 1.0 if i % 2 == 0 else 0.0
        out = safety.process({0: (0.0, 0.0, v)}, _DT)[0]
        if i % 2 == 0:
            lit.append(out)
    assert all(r < 0.05 and g < 0.05 and b > 0.05 for r, g, b in lit)


# --- gamut clamp + xy slew ----------------------------------------------

def test_clamp_keeps_every_colour_inside_gamut_c():
    rng = np.random.default_rng(0)
    for _ in range(500):
        x, y = float(rng.uniform(-0.2, 1.0)), float(rng.uniform(-0.2, 1.0))
        cx, cy = clamp_to_gamut(x, y)
        assert _point_in_triangle((cx, cy), *GAMUT_C)


def test_rgb_to_xy_outputs_are_in_gamut():
    for rgb in [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (0, 1, 1), (1, 0, 1), (1, 1, 1)]:
        assert _point_in_triangle(rgb_to_xy(*rgb), *GAMUT_C)


def test_xy_slew_limits_a_palette_jump():
    # A hard cut from deep red to deep blue must not pop: the first frame after
    # the jump moves only a bounded step in xy, not the whole way.
    enc = HueStreamEncoder("abcdefab-1234-1234-1234-0123456789ab")
    enc.build({0: (1.0, 0.0, 0.0)})  # establish red
    red = enc._prev_xy[0]
    enc.build({0: (0.0, 0.0, 1.0)})  # jump to blue
    blue = enc._prev_xy[0]
    step = math.dist(red, blue)
    assert 0.0 < step <= 0.08 + 1e-6  # capped, not an instant jump


# --- analyzer noise gate -------------------------------------------------

def test_noise_gate_rests_on_a_near_silent_signal():
    # A signal below the noise floor must not be AGC-amplified up to full: bands
    # and energy stay at rest and no beats fire.
    a = Analyzer()
    rng = np.random.default_rng(1)
    lit = 0
    for _ in range(300):
        hop = (rng.standard_normal(ANALYSIS_HOP) * 1e-4).astype(np.float32)  # ~-80 dBFS
        f = a.push(hop)
        lit += int(any(v > 0.05 for v in f.bands.values()) or f.energy > 0.05 or f.beat)
    assert lit == 0


def test_noise_gate_still_passes_real_signal():
    # A clearly audible tone must pass the gate (the gate only rests on silence).
    a = Analyzer()
    sr = 22050
    t = np.arange(ANALYSIS_HOP) / sr
    loud = 0
    for k in range(200):
        phase = 2 * np.pi * 200 * (t + k * ANALYSIS_HOP / sr)
        hop = (0.3 * np.sin(phase)).astype(np.float32)
        f = a.push(hop)
        loud += int(any(v > 0.1 for v in f.bands.values()))
    assert loud > 0
