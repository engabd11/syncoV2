"""Samsung-style intensity modes as parameters for one unified renderer.

Brightness is driven mainly by the **continuous bass envelope** so the lights
react to *every* kick proportionally (not just the big detected beats), with an
extra pop on detected beats and optional treble shimmer. Higher modes get darker
between beats and brighten harder on them — Intense is a dark club that snaps to
full on the beat; Subtle does no dimming and only drifts colour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..const import SyncMode
from ..color.palette import RGB


@dataclass(frozen=True, slots=True)
class ModeParams:
    base: float          # steady brightness between beats
    floor: float         # minimum brightness (darkness between beats)
    bass_gain: float     # continuous brightness from the bass envelope
    beat_gain: float     # pop on a (qualifying) beat
    beat_threshold: float  # only beats this strong pop (higher = big beats only)
    spread: float        # per-light spectrum variety (treble lights vs bass)
    colour_speed: float  # palette drift per second
    shimmer: float       # treble-driven sparkle amount


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # No dimming — only gentle colour drift.
    SyncMode.SUBTLE: ModeParams(
        base=1.0, floor=1.0, bass_gain=0.0, beat_gain=0.0, beat_threshold=9.0,
        spread=0.0, colour_speed=0.02, shimmer=0.0,
    ),
    # Dark, reacts to most beats (continuous bass + low threshold) — colourful.
    SyncMode.MEDIUM: ModeParams(
        base=0.10, floor=0.04, bass_gain=0.55, beat_gain=0.5, beat_threshold=1.0,
        spread=0.15, colour_speed=0.03, shimmer=0.06,
    ),
    # Mostly dark + shimmer; only the bigger beats flash.
    SyncMode.HIGH: ModeParams(
        base=0.07, floor=0.0, bass_gain=0.18, beat_gain=0.95, beat_threshold=1.5,
        spread=0.18, colour_speed=0.045, shimmer=0.22,
    ),
    # Club: near-dark + shimmer between beats; only BIG beats snap to full.
    SyncMode.INTENSE: ModeParams(
        base=0.05, floor=0.0, bass_gain=0.10, beat_gain=1.0, beat_threshold=1.8,
        spread=0.12, colour_speed=0.06, shimmer=0.4,
    ),
}


_BAND_ORDER = ["sub_bass", "bass", "low_mid", "mid", "high"]


def band_for_rank(rank: int, count: int) -> str:
    """Assign a frequency band to a channel given its left-to-right rank."""
    if count <= 1:
        return "bass"
    idx = int(rank / count * len(_BAND_ORDER))
    return _BAND_ORDER[min(idx, len(_BAND_ORDER) - 1)]


def _shimmer(t: float, cid: int) -> float:
    """Fast, per-channel pseudo-random sparkle in 0..1."""
    return 0.5 + 0.5 * math.sin(t * 23.0 + cid * 2.7) * math.sin(t * 8.0 + cid * 1.3)


def beat_flash(params: ModeParams, frame) -> float:
    """Flash amount a qualifying beat contributes (0 if it doesn't qualify).

    The engine maintains this as a fast-decaying overlay so beats always snap to
    full, independent of the slower continuous-brightness smoothing.
    """
    if frame.beat and frame.beat_strength >= params.beat_threshold:
        return params.beat_gain * min(1.0, frame.beat_strength / 2.0)
    return 0.0


def render(engine, frame) -> dict[int, tuple[RGB, float]]:
    """Per-channel (colour, continuous brightness) — no beat flash (added later)."""
    p: ModeParams = engine.params
    t = engine.time
    bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
    treble = frame.bands.get("high", 0.0)

    out: dict[int, tuple[RGB, float]] = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        bri = p.base + p.bass_gain * bass
        if p.spread:
            bri += p.spread * frame.bands.get(info["band"], 0.0)
        if p.shimmer and treble > 0.05:
            bri += p.shimmer * treble * _shimmer(t, ch.channel_id)

        bri = p.floor if bri < p.floor else 1.0 if bri > 1.0 else bri
        colour = engine.palette.sample(info["xrank"] + p.colour_speed * t)
        out[ch.channel_id] = (colour, bri)

    return out
