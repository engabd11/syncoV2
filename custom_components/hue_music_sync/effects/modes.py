"""Samsung-style intensity modes as parameters for one unified renderer.

Instead of separate choreographies, every mode is the same render driven by a
:class:`ModeParams`: a steady base brightness, a minimum floor (how far it may
dim), how much continuous band energy and discrete beats brighten lights, and an
optional treble shimmer. This mirrors Samsung's Subtle/Medium/High/Intense ladder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..const import SyncMode
from ..color.palette import RGB


@dataclass(frozen=True, slots=True)
class ModeParams:
    base: float          # steady brightness level
    floor: float         # minimum brightness (dimming limit)
    energy_gain: float   # continuous band/overall energy -> brightness
    beat_gain: float     # extra brightness on a beat
    band_reactive: bool  # per-light frequency band (else overall energy)
    bass_beats_only: bool  # only bass-positioned lights react to beats
    colour_speed: float  # palette drift per second
    shimmer: float       # treble-driven sparkle amount


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # No dimming, colours drift slowly.
    SyncMode.SUBTLE: ModeParams(
        base=1.0, floor=1.0, energy_gain=0.0, beat_gain=0.0,
        band_reactive=False, bass_beats_only=False, colour_speed=0.012, shimmer=0.0,
    ),
    # Stays bright; some (bass) lights pulse up on beats.
    SyncMode.MEDIUM: ModeParams(
        base=0.92, floor=0.88, energy_gain=0.06, beat_gain=0.25,
        band_reactive=False, bass_beats_only=True, colour_speed=0.018, shimmer=0.0,
    ),
    # Dims no lower than ~30%, bright beats on bass + treble.
    SyncMode.HIGH: ModeParams(
        base=0.55, floor=0.30, energy_gain=0.40, beat_gain=0.45,
        band_reactive=True, bass_beats_only=False, colour_speed=0.03, shimmer=0.18,
    ),
    # Full 0-100% dimming/brightening with shimmer.
    SyncMode.INTENSE: ModeParams(
        base=0.22, floor=0.0, energy_gain=0.75, beat_gain=0.65,
        band_reactive=True, bass_beats_only=False, colour_speed=0.05, shimmer=0.5,
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
    high = frame.bands.get("high", 0.0)
    overall = frame.energy
    beat = frame.beat
    strength = min(1.0, frame.beat_strength)

    out: dict[int, tuple[RGB, float]] = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        bri = p.base

        if p.energy_gain:
            level = frame.bands.get(info["band"], 0.0) if p.band_reactive else overall
            bri += p.energy_gain * level

        if p.beat_gain and beat:
            if not p.bass_beats_only or info["band"] in ("sub_bass", "bass"):
                bri += p.beat_gain * strength

        if p.shimmer and high > 0.05:
            bri += p.shimmer * high * _shimmer(t, ch.channel_id)

        bri = p.floor if bri < p.floor else 1.0 if bri > 1.0 else bri
        colour = engine.palette.sample(info["xrank"] + p.colour_speed * t)
        out[ch.channel_id] = (colour, bri)

    return out
