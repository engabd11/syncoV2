"""Per-mode target computation.

Each mode is a pure function of the engine's precomputed channel map, the active
palette and the current :class:`AnalysisFrame`. It returns, per channel id, a
``(base_color, brightness)`` target where ``base_color`` is a full-intensity RGB
and ``brightness`` is 0..1. The engine handles smoothing, intensity scaling and
frame encoding, so modes stay declarative.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..audio.analyzer import AnalysisFrame
from ..color.palette import RGB
from ..const import EffectMode

if TYPE_CHECKING:
    from .engine import EffectEngine

Target = dict[int, tuple[RGB, float]]

_BAND_ORDER = ["sub_bass", "bass", "low_mid", "mid", "high"]
_FLOOR = 0.06  # never fully dark while syncing


def render_pulse(engine: EffectEngine, frame: AnalysisFrame) -> Target:
    """Whole-area pulse: one slowly-rotating colour, brightness on the beat."""
    punch = max(frame.energy, frame.bands.get("bass", 0.0))
    base_b = _FLOOR + 0.55 * punch
    if frame.beat:
        base_b = min(1.0, base_b + 0.35 * min(1.0, frame.beat_strength))
    out: Target = {}
    for ch in engine.channels:
        phase = engine.color_phase + engine.cmap[ch.channel_id]["xrank"] * 0.18
        out[ch.channel_id] = (engine.palette.sample(phase), base_b)
    return out


def render_spectrum(engine: EffectEngine, frame: AnalysisFrame) -> Target:
    """Each light owns a frequency band (by x position) and a palette colour."""
    out: Target = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        band = info["band"]
        level = frame.bands.get(band, 0.0)
        # Low bands get a beat kick so kicks really thump the bass lights.
        if frame.beat and band in ("sub_bass", "bass"):
            level = min(1.0, level + 0.4 * min(1.0, frame.beat_strength))
        color = engine.palette.sample(info["xrank"])
        out[ch.channel_id] = (color, _FLOOR + (1.0 - _FLOOR) * level)
    return out


def render_wave(engine: EffectEngine, frame: AnalysisFrame) -> Target:
    """A travelling wavefront sweeps across x; beats brighten it."""
    # Wave speed loosely tracks tempo (default ~1 sweep/sec).
    speed = (frame.tempo_bpm or 120.0) / 120.0
    front = (engine.time * speed) % 1.0
    gate = _FLOOR + 0.5 * frame.energy
    if frame.beat:
        gate = min(1.0, gate + 0.3 * min(1.0, frame.beat_strength))
    out: Target = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        d = abs(info["norm_x"] - front)
        d = min(d, 1.0 - d)  # wrap-around distance
        intensity = math.exp(-(d * d) / (2 * 0.12**2))
        color = engine.palette.sample(info["norm_x"] + engine.color_phase)
        out[ch.channel_id] = (color, _FLOOR + (gate - _FLOOR) * intensity + 0.25 * gate * intensity)
    return out


def render_ambient(engine: EffectEngine, frame: AnalysisFrame) -> Target:
    """Slow palette drift, gentle energy modulation, no hard beats."""
    out: Target = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        color = engine.palette.sample(engine.color_phase * 0.3 + info["norm_x"] * 0.4)
        out[ch.channel_id] = (color, _FLOOR + 0.35 * frame.energy)
    return out


RENDERERS = {
    EffectMode.PULSE: render_pulse,
    EffectMode.SPECTRUM: render_spectrum,
    EffectMode.WAVE: render_wave,
    EffectMode.AMBIENT: render_ambient,
}


def band_for_rank(rank: int, count: int) -> str:
    """Assign a frequency band to a channel given its left-to-right rank."""
    if count <= 1:
        return "bass"
    idx = int(rank / count * len(_BAND_ORDER))
    return _BAND_ORDER[min(idx, len(_BAND_ORDER) - 1)]
