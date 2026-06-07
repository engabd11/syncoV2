"""The choreography engine: features + palette + mode params -> per-channel RGB.

Owns per-channel smoothing for natural motion (fast attack on beats, gentle
decay), advances a colour-drift phase over time, and renders via the single
parametric :func:`.modes.render` driven by the active mode. Output is a
``{channel_id: (r, g, b)}`` map of 0..1 colours for the xy+brightness encoder.
"""

from __future__ import annotations

from ..audio.analyzer import AnalysisFrame
from ..color.palette import RGB, Palette, get_palette
from ..const import DEFAULT_MODE, ColorScheme, SyncMode
from ..hue.bridge import EntertainmentChannel
from .modes import MODE_PARAMS, band_for_rank, beat_flash, render

_FLASH_DECAY = 0.80  # per-frame fade of the beat flash overlay (~5 frames)

# Brightness smoothing: snap up hard on beats, fall back quickly so it goes dark
# between beats (club feel). Colour eases slowly for smooth shifting.
_ATTACK = 0.92
_DECAY = 0.24
_COLOR_LERP = 0.16

# Pleasant fallback palette when no album art is available (e.g. live radio).
_FALLBACK_SCHEME = ColorScheme.SUNSET


class EffectEngine:
    """Renders entertainment frames from audio features and the active mode."""

    def __init__(self, channels: list[EntertainmentChannel]) -> None:
        self.palette: Palette = get_palette(_FALLBACK_SCHEME)
        self.params = MODE_PARAMS[DEFAULT_MODE]
        self.brightness = 1.0  # master ceiling (0..1), independent of mode
        self.time: float = 0.0
        self._flash = 0.0  # beat-flash overlay (decays fast)
        self.set_channels(channels)

    def set_channels(self, channels: list[EntertainmentChannel]) -> None:
        self.channels = channels
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

    def set_mode(self, mode: SyncMode) -> None:
        self.params = MODE_PARAMS[mode]

    def set_brightness(self, brightness: float) -> None:
        """Master brightness ceiling (0..1), scaling the mode's output."""
        self.brightness = max(0.0, min(1.0, brightness))

    def render_idle(self, phase: float, level: float = 0.12) -> dict[int, RGB]:
        """A gentle, mode-independent palette glow for paused/idle state."""
        dim = level * self.brightness
        out: dict[int, RGB] = {}
        for ch in self.channels:
            info = self.cmap[ch.channel_id]
            c = self.palette.sample(info["xrank"] + phase)
            m = max(c)
            nc = (c[0] / m, c[1] / m, c[2] / m) if m > 1e-6 else (0.0, 0.0, 0.0)
            out[ch.channel_id] = (nc[0] * dim, nc[1] * dim, nc[2] * dim)
        return out

    def render(self, frame: AnalysisFrame, dt: float) -> dict[int, RGB]:
        """Advance time and produce smoothed per-channel RGB (0..1).

        Colour (full-value chromaticity) and brightness are smoothed separately;
        the colour is renormalised to max-channel 1 so brightness is carried
        purely by ``new_b``. That keeps mode floors honest (the encoder reads
        brightness as the max channel) even mid colour-transition.
        """
        self.time += dt
        # Beat-flash overlay: snaps to full on a qualifying beat, then decays
        # fast — independent of the slower continuous-brightness smoothing.
        self._flash = max(self._flash * _FLASH_DECAY, beat_flash(self.params, frame))
        targets = render(self, frame)

        out: dict[int, RGB] = {}
        for cid, (target_color, target_b) in targets.items():
            prev_color, prev_b = self._state[cid]
            alpha = _ATTACK if target_b >= prev_b else _DECAY
            new_b = prev_b + (target_b - prev_b) * alpha
            blended = (
                prev_color[0] + (target_color[0] - prev_color[0]) * _COLOR_LERP,
                prev_color[1] + (target_color[1] - prev_color[1]) * _COLOR_LERP,
                prev_color[2] + (target_color[2] - prev_color[2]) * _COLOR_LERP,
            )
            m = max(blended)
            nc = (blended[0] / m, blended[1] / m, blended[2] / m) if m > 1e-6 else (0.0, 0.0, 0.0)
            self._state[cid] = (nc, new_b)
            # Soften colours toward white per the mode (keeps max channel = 1).
            sat = self.params.colour_sat
            if sat < 1.0:
                nc = (nc[0] * sat + (1.0 - sat), nc[1] * sat + (1.0 - sat), nc[2] * sat + (1.0 - sat))
            # Continuous brightness + sharp flash, then master-brightness scaling.
            b = min(1.0, new_b + self._flash) * self.brightness
            out[cid] = (nc[0] * b, nc[1] * b, nc[2] * b)
        return out
