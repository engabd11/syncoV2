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
    colour_sat: float = 1.0  # <1 softens colours toward white (Samsung-style)


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # Stays bright and colourful, but gently sways with the music + colour drift.
    SyncMode.SUBTLE: ModeParams(
        base=0.82, floor=0.62, bass_gain=0.18, beat_gain=0.0, beat_threshold=9.0,
        spread=0.05, colour_speed=0.025, shimmer=0.0, colour_sat=1.0,
    ),
    # Like Intense but lighter: brighter baseline, gentler pulses on most beats,
    # more colour.
    SyncMode.MEDIUM: ModeParams(
        base=0.22, floor=0.14, bass_gain=0.28, beat_gain=0.55, beat_threshold=1.1,
        spread=0.15, colour_speed=0.04, shimmer=0.10, colour_sat=0.8,
    ),
    # "Heavy": immersive Intense-style, but flashes ONLY on big beats (eye-
    # friendly, less strobing) over a sustained lit base.
    SyncMode.HIGH: ModeParams(
        base=0.30, floor=0.20, bass_gain=0.20, beat_gain=0.8, beat_threshold=2.2,
        spread=0.12, colour_speed=0.05, shimmer=0.12, colour_sat=0.6,
    ),
    # Samsung-style with visible dimming: dim baseline that clearly drops between
    # beats and pulses up on the main beats (selective so the flash decays). Soft
    # desaturated album colours.
    SyncMode.INTENSE: ModeParams(
        base=0.10, floor=0.05, bass_gain=0.12, beat_gain=0.9, beat_threshold=1.6,
        spread=0.12, colour_speed=0.05, shimmer=0.2, colour_sat=0.5,
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

    Weighted by bass content so the flashes track the kick/bass beat rather than
    incidental onsets (hi-hats etc.). The engine keeps this as a fast-decaying
    overlay so beats always snap to full, independent of the slower continuous
    brightness smoothing.
    """
    if frame.beat and frame.beat_strength >= params.beat_threshold:
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
        weight = 0.4 + 0.6 * bass  # full on kicks, dimmer on bass-less onsets
        return params.beat_gain * min(1.0, frame.beat_strength / 2.0) * weight
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
