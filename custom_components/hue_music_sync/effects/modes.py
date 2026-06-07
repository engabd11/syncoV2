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
    bass_gain: float     # brightness from the bass envelope (catches most beats)
    beat_gain: float     # extra pop on a detected beat
    spread: float        # per-light spectrum variety (treble lights vs bass lights)
    colour_speed: float  # palette drift per second
    shimmer: float       # treble-driven sparkle amount


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # No dimming — only gentle colour drift.
    SyncMode.SUBTLE: ModeParams(
        base=1.0, floor=1.0, bass_gain=0.0, beat_gain=0.0,
        spread=0.0, colour_speed=0.02, shimmer=0.0,
    ),
    # Dark, reacts to most beats, moderate pops — gentle, colourful.
    SyncMode.MEDIUM: ModeParams(
        base=0.10, floor=0.04, bass_gain=0.65, beat_gain=0.35,
        spread=0.15, colour_speed=0.03, shimmer=0.0,
    ),
    # Darker, stronger beat pops, a touch of shimmer.
    SyncMode.HIGH: ModeParams(
        base=0.08, floor=0.02, bass_gain=0.85, beat_gain=0.60,
        spread=0.25, colour_speed=0.045, shimmer=0.15,
    ),
    # Club: near-dark between beats, snaps to full on the beat, shimmer on highs.
    SyncMode.INTENSE: ModeParams(
        base=0.03, floor=0.0, bass_gain=1.0, beat_gain=0.95,
        spread=0.20, colour_speed=0.06, shimmer=0.4,
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


def render(engine, frame) -> dict[int, tuple[RGB, float]]:
    """Return per-channel (base_colour, brightness) for the active mode params."""
    p: ModeParams = engine.params
    t = engine.time
    bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
    treble = frame.bands.get("high", 0.0)
    beat = frame.beat
    strength = min(1.0, frame.beat_strength)

    out: dict[int, tuple[RGB, float]] = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        bri = p.base
        # Continuous bass envelope -> reacts to every kick.
        bri += p.bass_gain * bass
        # Extra pop on detected beats.
        if beat:
            bri += p.beat_gain * strength
        # Per-light spectrum variety (treble-side lights catch highs, etc.).
        if p.spread:
            bri += p.spread * frame.bands.get(info["band"], 0.0)
        # Treble sparkle.
        if p.shimmer and treble > 0.05:
            bri += p.shimmer * treble * _shimmer(t, ch.channel_id)

        bri = p.floor if bri < p.floor else 1.0 if bri > 1.0 else bri
        colour = engine.palette.sample(info["xrank"] + p.colour_speed * t)
        out[ch.channel_id] = (colour, bri)

    return out
