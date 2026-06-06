"""The choreography engine: features + palette + positions -> per-channel RGB.

Owns the per-channel smoothing so motion looks natural (fast attack on beats,
gentle decay), advances the colour phase over time, and dispatches to the active
:mod:`.modes` renderer. Output is a ``{channel_id: (r, g, b)}`` map of normalised
0..1 colours ready for :class:`~..hue.stream.HueStreamEncoder`.
"""

from __future__ import annotations

from ..audio.analyzer import AnalysisFrame
from ..const import (
    DEFAULT_COLOR_SCHEME,
    DEFAULT_EFFECT_MODE,
    DEFAULT_INTENSITY,
    ColorScheme,
    EffectMode,
)
from ..color.palette import RGB, Palette, get_palette
from ..hue.bridge import EntertainmentChannel
from .modes import RENDERERS, band_for_rank

# Brightness smoothing: snap up on transients, ease back down.
_ATTACK = 0.7
_DECAY = 0.14
_COLOR_LERP = 0.25
_PHASE_SPEED = 0.025  # palette rotations per second (baseline)


class EffectEngine:
    """Renders entertainment frames from audio features."""

    def __init__(self, channels: list[EntertainmentChannel]) -> None:
        self.palette: Palette = get_palette(DEFAULT_COLOR_SCHEME)
        self.mode: EffectMode = DEFAULT_EFFECT_MODE
        self.intensity: float = DEFAULT_INTENSITY
        self.time: float = 0.0
        self.color_phase: float = 0.0
        self.set_channels(channels)

    def set_channels(self, channels: list[EntertainmentChannel]) -> None:
        self.channels = channels
        # Rank channels left-to-right by x for band/colour spread.
        order = sorted(channels, key=lambda c: c.x)
        n = len(order)
        self.cmap: dict[int, dict] = {}
        for rank, ch in enumerate(order):
            self.cmap[ch.channel_id] = {
                "norm_x": (ch.x + 1.0) / 2.0,
                "xrank": rank / max(1, n - 1),
                "band": band_for_rank(rank, n),
            }
        self._state: dict[int, tuple[RGB, float]] = {
            ch.channel_id: ((0.0, 0.0, 0.0), 0.0) for ch in channels
        }

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette

    def set_scheme(self, scheme: ColorScheme) -> None:
        self.palette = get_palette(scheme)

    def set_mode(self, mode: EffectMode) -> None:
        self.mode = mode

    def set_intensity(self, intensity: float) -> None:
        self.intensity = max(0.0, min(1.5, intensity))

    def render(self, frame: AnalysisFrame, dt: float) -> dict[int, RGB]:
        """Advance time and produce smoothed per-channel RGB (0..1)."""
        self.time += dt
        speed = _PHASE_SPEED * (1.0 + frame.energy)
        self.color_phase += dt * speed
        if frame.beat and self.mode == EffectMode.PULSE:
            self.color_phase += 0.04 * min(1.0, frame.beat_strength)

        targets = RENDERERS[self.mode](self, frame)

        out: dict[int, RGB] = {}
        for cid, (target_color, target_b) in targets.items():
            prev_color, prev_b = self._state[cid]
            # Asymmetric brightness smoothing.
            alpha = _ATTACK if target_b >= prev_b else _DECAY
            new_b = prev_b + (target_b - prev_b) * alpha
            # Colour easing.
            new_color = (
                prev_color[0] + (target_color[0] - prev_color[0]) * _COLOR_LERP,
                prev_color[1] + (target_color[1] - prev_color[1]) * _COLOR_LERP,
                prev_color[2] + (target_color[2] - prev_color[2]) * _COLOR_LERP,
            )
            self._state[cid] = (new_color, new_b)
            scale = new_b * self.intensity
            out[cid] = (
                min(1.0, new_color[0] * scale),
                min(1.0, new_color[1] * scale),
                min(1.0, new_color[2] * scale),
            )
        return out
