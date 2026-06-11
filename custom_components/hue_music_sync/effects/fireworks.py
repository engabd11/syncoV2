"""Fireworks effect: bursts of colour ignite on big beats and fade out.

A dark area where, on each qualifying beat, one or more random lights "launch" —
snapping to a vivid palette colour at full brightness — then decay back to near
black over a fraction of a second, like a firework trail. The selected palette
(album art or a preset) supplies the burst colours and the active intensity mode
controls how many launch at once, how fast they fade and how dark the base sits.

Unlike the music renderer this owns its own per-light state and decay, so it is
deliberately *not* run through the engine's brightness smoothing — the snap-and-
fade is the whole point.
"""

from __future__ import annotations

import math
import random

from ..color.palette import RGB

# Fade time constants (seconds) for the burst trail, fastest..slowest intensity.
# Indexed by the mode's beat_threshold band; resolved in :meth:`_tau` below.
_TAU_FAST = 0.30
_TAU_SLOW = 0.55

# Auto-launch a burst if this long passes with no qualifying beat, so quiet
# passages still sparkle instead of going fully dark.
_AUTO_LAUNCH_S = 1.4


class FireworksEffect:
    """Stateful per-channel burst renderer driven by detected beats."""

    def __init__(self, seed: int | None = None) -> None:
        self._state: dict[int, RGB] = {}
        self._since_launch = 0.0
        self._rng = random.Random(seed)

    def reset(self) -> None:
        self._state.clear()
        self._since_launch = 0.0

    def _tau(self, params) -> float:
        # Lower beat_threshold (more reactive modes) -> slightly faster, snappier
        # fades; gentler modes hold the glow a touch longer.
        if params.beat_threshold <= 1.2:
            return _TAU_FAST
        if params.beat_threshold >= 2.2:
            return _TAU_SLOW
        return 0.5 * (_TAU_FAST + _TAU_SLOW)

    def _launch_count(self, channel_count: int, strength: float, params) -> int:
        """How many lights ignite on this beat (>=1), scaled by beat strength."""
        base = 1 + int(strength)  # bigger beats burst wider
        cap = max(1, channel_count // 2 if params.beat_threshold > 1.2 else channel_count)
        return max(1, min(base, cap))

    def render(self, engine, frame, dt: float) -> dict[int, RGB]:
        """Return final per-channel RGB (already master-brightness scaled)."""
        params = engine.params
        channels = engine.channels
        # Frame-rate-independent exponential fade of every live burst.
        fade = math.exp(-dt / self._tau(params))
        for cid in list(self._state):
            r, g, b = self._state[cid]
            self._state[cid] = (r * fade, g * fade, b * fade)

        # Decide whether to ignite new bursts this frame (kick/bass onsets only,
        # so vocals and hi-hats don't launch fireworks).
        self._since_launch += dt
        qualifying = frame.bass_beat and frame.bass_strength >= params.beat_threshold
        if qualifying or self._since_launch >= _AUTO_LAUNCH_S:
            self._since_launch = 0.0
            strength = frame.bass_strength if qualifying else 1.0
            self._launch(engine, channels, strength, params)

        floor = params.floor * 0.25  # faint palette ember so it's never pitch black
        out: dict[int, RGB] = {}
        for ch in channels:
            cid = ch.channel_id
            burst = self._state.get(cid, (0.0, 0.0, 0.0))
            if floor > 0.0:
                info = engine.cmap[cid]
                gc = engine.palette.sample(info["xrank"] + engine.colour_phase)
                m = max(gc) or 1.0
                glow = (gc[0] / m * floor, gc[1] / m * floor, gc[2] / m * floor)
                burst = (max(burst[0], glow[0]), max(burst[1], glow[1]), max(burst[2], glow[2]))
            mb = engine.brightness
            out[cid] = (burst[0] * mb, burst[1] * mb, burst[2] * mb)
        return out

    def _launch(self, engine, channels, strength: float, params) -> None:
        if not channels:
            return
        n = self._launch_count(len(channels), strength, params)
        picked = self._rng.sample(channels, k=min(n, len(channels)))
        for ch in picked:
            # Draw a vivid colour from the palette at a random position so bursts
            # vary while staying on-theme; normalise to full brightness.
            colour = engine.palette.sample(self._rng.random())
            m = max(colour)
            if m <= 1e-6:
                colour = (1.0, 1.0, 1.0)
            else:
                colour = (colour[0] / m, colour[1] / m, colour[2] / m)
            self._state[ch.channel_id] = colour
