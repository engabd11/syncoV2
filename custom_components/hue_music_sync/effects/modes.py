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
    colour_speed: float  # palette drift per second (continuous, time-based)
    shimmer: float       # treble-driven sparkle amount
    colour_sat: float = 1.0  # <1 softens colours toward white (Samsung-style)
    colour_beat_step: float = 0.0  # palette advance per beat (colour moves to the beat)
    colour_lerp: float = 0.16  # per-frame colour easing (higher = snappier shifts)
    energy_gain: float = 0.0  # brightness from broadband loudness (ambient/movie)
    bri_attack: float = 0.92  # per-frame brightness rise rate (1 = instant)
    bri_decay: float = 0.24  # per-frame brightness fall rate (lower = gentler)
    # --- 3D spatial choreography (0 = off, keeps the flat/legacy look) -------
    wave_gain: float = 0.0     # brightness from beat wavefronts sweeping the room
    wave_speed: float = 1.8    # wavefront speed (normalised room-units / second)
    wave_width: float = 0.33   # wavefront shell thickness
    height_freq: float = 0.0   # how much a lamp's height maps to its frequency band
    depth_wash: float = 0.0    # gentle ambient wash on the back (far) lamps
    anticipation_ms: float = 0.0  # fire the wave this early to peak on the beat
    # --- musical structure response -----------------------------------------
    drop_boost: float = 0.0    # extra swell on a detected drop
    build_desat: float = 0.0   # desaturate toward white through a build (tension)
    warm_calm: float = 0.0     # pull colour toward warm white in quiet moments


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # Seamless: lights hold a steady, bright level and simply let the colour flow
    # and shift smoothly across the room. No beat flashes and no stepping — only a
    # continuous colour drift plus a very gentle, heavily-eased breath with the
    # music's energy. The calmest preset.
    SyncMode.SUBTLE: ModeParams(
        base=0.80, floor=0.72, bass_gain=0.10, beat_gain=0.0, beat_threshold=99.0,
        spread=0.0, colour_speed=0.03, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.0, colour_lerp=0.05, bri_attack=0.12, bri_decay=0.08,
    ),
    # Gentle: stays bright and colourful and sways softly with the music, with a
    # slow colour drift nudged a touch on each beat. The brightness sway is eased
    # smoothly both ways so it breathes rather than pulses — no flashing and no
    # wavefronts, a relaxed glow one notch livelier than Subtle.
    SyncMode.MEDIUM: ModeParams(
        base=0.80, floor=0.66, bass_gain=0.18, beat_gain=0.0, beat_threshold=9.0,
        spread=0.05, colour_speed=0.025, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.004, colour_lerp=0.08, bri_attack=0.30, bri_decay=0.12,
    ),
    # "Heavy": immersive Intense-style, but the beat travels as a wavefront
    # (eye-friendly spatial distribution rather than synchronous strobing) over a
    # sustained lit base.
    SyncMode.HIGH: ModeParams(
        base=0.30, floor=0.20, bass_gain=0.20, beat_gain=0.8, beat_threshold=2.2,
        spread=0.12, colour_speed=0.05, shimmer=0.12, colour_sat=0.6,
        colour_beat_step=0.024, colour_lerp=0.22,
        wave_gain=0.60, wave_speed=1.9, wave_width=0.33, height_freq=0.40,
        depth_wash=0.15, anticipation_ms=70, drop_boost=0.35, build_desat=0.40,
    ),
    # Samsung-style with visible dimming: dim baseline that clearly drops between
    # beats; the kick launches a strong wavefront across the room (and a small
    # residual flash). Soft desaturated album colours; colour jumps hardest per
    # beat for a club feel.
    SyncMode.INTENSE: ModeParams(
        base=0.10, floor=0.05, bass_gain=0.12, beat_gain=0.9, beat_threshold=1.6,
        spread=0.12, colour_speed=0.05, shimmer=0.2, colour_sat=0.5,
        colour_beat_step=0.034, colour_lerp=0.30,
        wave_gain=0.85, wave_speed=2.2, wave_width=0.30, height_freq=0.50,
        depth_wash=0.10, anticipation_ms=80, drop_boost=0.50, build_desat=0.50,
    ),
    # Club: the maximum, and unmistakably so. Unlike Intense (a lit, immersive
    # baseline), Extreme keeps the room *dark* and fires vivid colour beams that
    # snap to full and sweep across the lamps on every beat — a strobe-like
    # wavefront in a black room, with hard fast colour jumps and treble sparkle.
    # The darkness is the point: there's no continuous baseline (base/height/depth
    # ~0), so each light slams from black to full as the wave passes and drops
    # fast. The beams are spatially distributed, so it stays under the WCAG
    # full-field flash ceiling while feeling far harder than Intense.
    SyncMode.EXTREME: ModeParams(
        base=0.0, floor=0.0, bass_gain=0.06, beat_gain=1.0, beat_threshold=1.0,
        spread=0.0, colour_speed=0.12, shimmer=0.35, colour_sat=0.92,
        colour_beat_step=0.070, colour_lerp=0.50,
        wave_gain=1.4, wave_speed=3.2, wave_width=0.25, height_freq=0.0,
        depth_wash=0.0, anticipation_ms=90, drop_boost=1.0, build_desat=0.60,
        bri_attack=1.0, bri_decay=0.50,
    ),
}


# Parameters for the Movies *effect* (not part of the intensity ladder).
# Deliberately calm so it never pulls your eye from the screen: brightness gently
# follows the soundtrack's overall loudness (no beat flashes, no shimmer), colour
# drifts slowly through the artwork palette, softened toward white, and eases
# slowly both ways so even explosions swell rather than strobe. Pair with the
# "Album colours" theme (the default) to pull colours from the film's artwork.
MOVIE_PARAMS = ModeParams(
    base=0.28, floor=0.16, bass_gain=0.0, beat_gain=0.0, beat_threshold=99.0,
    spread=0.0, colour_speed=0.012, shimmer=0.0, colour_sat=0.6,
    colour_beat_step=0.0, colour_lerp=0.05, energy_gain=0.5,
    bri_attack=0.16, bri_decay=0.07, warm_calm=0.45,
)


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


def beat_colour_advance(params: ModeParams, frame) -> float:
    """Extra palette phase to add on this frame's beat (0 if no beat).

    This is what makes the colour *move with the music* rather than only drifting
    on a timer: every detected beat nudges the whole palette forward, weighted by
    how strong the beat is and how much bass it carries, so the colour visibly
    steps on the kick. Any beat counts (not just the big ``beat_threshold`` ones)
    so the colour keeps grooving even in quieter sections.
    """
    if not frame.beat or params.colour_beat_step <= 0.0:
        return 0.0
    bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
    weight = 0.5 + 0.5 * bass
    return params.colour_beat_step * min(1.5, 0.5 + frame.beat_strength) * weight


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
    p: ModeParams = engine.active_params
    t = engine.time
    bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
    treble = frame.bands.get("high", 0.0)

    waves = engine.active_waves
    out: dict[int, tuple[RGB, float]] = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        bri = p.base + p.bass_gain * bass
        if p.energy_gain:
            bri += p.energy_gain * frame.energy  # follow overall loudness (movie)
        if p.spread:
            bri += p.spread * frame.bands.get(info["band"], 0.0)
        if p.height_freq:
            # Lamps high in the room favour treble, low lamps favour bass.
            bri += p.height_freq * frame.bands.get(info["hband"], 0.0)
        if p.depth_wash:
            # Back/far lamps carry a gentle ambient wash; front lamps stay reactive.
            bri += p.depth_wash * (1.0 - info["ny"])
        if p.wave_gain and waves:
            # Beat wavefront(s) sweeping out from the room's low centre.
            d = info["dist_origin"]
            amp = 0.0
            for w in waves:
                amp += w.amplitude_at(d)
            bri += p.wave_gain * amp
        if p.shimmer and treble > 0.05:
            bri += p.shimmer * treble * _shimmer(t, ch.channel_id)

        bri = p.floor if bri < p.floor else 1.0 if bri > 1.0 else bri
        # Palette position = this light's spatial rank + the engine's accumulated
        # colour phase (time drift + per-beat steps), so colour moves to the beat.
        colour = engine.palette.sample(info["xrank"] + engine.colour_phase)
        out[ch.channel_id] = (colour, bri)

    return out
