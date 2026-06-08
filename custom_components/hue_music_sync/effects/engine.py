"""The choreography engine: features + palette + mode params -> per-channel RGB.

Owns per-channel smoothing for natural motion (fast attack on beats, gentle
decay), advances a colour-drift phase over time, and renders via the single
parametric :func:`.modes.render` driven by the active mode. Output is a
``{channel_id: (r, g, b)}`` map of 0..1 colours for the xy+brightness encoder.
"""

from __future__ import annotations

from ..audio.analyzer import AnalysisFrame
from ..color.palette import RGB, Palette, get_palette
from ..const import DEFAULT_EFFECT, DEFAULT_MODE, ColorScheme, SyncEffect, SyncMode
from ..hue.bridge import EntertainmentChannel
from .fireworks import FireworksEffect
from .modes import (
    MODE_PARAMS,
    MOVIE_PARAMS,
    band_for_rank,
    beat_colour_advance,
    beat_flash,
    render,
)

_FLASH_DECAY = 0.80  # per-frame fade of the beat flash overlay (~5 frames)

# Brightness and colour smoothing are per-mode (ModeParams.bri_attack/bri_decay
# and colour_lerp): club modes snap up hard and fall fast; Movie eases gently.

# Pleasant fallback palette when no album art is available (e.g. live radio).
_FALLBACK_SCHEME = ColorScheme.SUNSET


class EffectEngine:
    """Renders entertainment frames from audio features and the active mode."""

    def __init__(self, channels: list[EntertainmentChannel]) -> None:
        self.palette: Palette = get_palette(_FALLBACK_SCHEME)
        self.params = MODE_PARAMS[DEFAULT_MODE]
        self.effect: SyncEffect = DEFAULT_EFFECT
        self.brightness = 1.0  # master ceiling (0..1), independent of mode
        self.time: float = 0.0
        self.colour_phase: float = 0.0  # palette position: time drift + beat steps
        self._flash = 0.0  # beat-flash overlay (decays fast)
        self._fireworks = FireworksEffect()
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

    def set_effect(self, effect: SyncEffect) -> None:
        if effect != self.effect:
            self._fireworks.reset()  # start the new renderer from a clean slate
        self.effect = effect

    def set_brightness(self, brightness: float) -> None:
        """Master brightness ceiling (0..1), scaling the mode's output."""
        self.brightness = max(0.0, min(1.0, brightness))

    @property
    def active_params(self):
        """Render params for the current effect.

        The Movies effect uses its own calm preset regardless of the selected
        intensity; every other effect uses the chosen intensity's params.
        """
        return MOVIE_PARAMS if self.effect is SyncEffect.MOVIES else self.params

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
        """Advance time and produce per-channel RGB (0..1) for the active effect."""
        self.time += dt
        if self.effect is SyncEffect.FIREWORKS:
            # Fireworks owns its own per-light snap/fade, so it bypasses the music
            # smoothing pipeline; it still advances colour_phase for its ember glow.
            self.colour_phase += self.active_params.colour_speed * dt
            return self._fireworks.render(self, frame, dt)
        return self._render_music(frame, dt)

    def _render_music(self, frame: AnalysisFrame, dt: float) -> dict[int, RGB]:
        """Smoothed beat/frequency choreography (Music and Movies effects).

        Colour (full-value chromaticity) and brightness are smoothed separately;
        the colour is renormalised to max-channel 1 so brightness is carried
        purely by ``new_b``. That keeps mode floors honest (the encoder reads
        brightness as the max channel) even mid colour-transition.
        """
        p = self.active_params
        # Advance the palette position: a slow continuous drift plus a step on
        # every beat so the colour visibly moves with the music.
        self.colour_phase += p.colour_speed * dt
        if frame.beat:
            self.colour_phase += beat_colour_advance(p, frame)
        # Beat-flash overlay: snaps to full on a qualifying beat, then decays
        # fast — independent of the slower continuous-brightness smoothing.
        self._flash = max(self._flash * _FLASH_DECAY, beat_flash(p, frame))
        targets = render(self, frame)

        colour_lerp = p.colour_lerp
        attack, decay = p.bri_attack, p.bri_decay
        out: dict[int, RGB] = {}
        for cid, (target_color, target_b) in targets.items():
            prev_color, prev_b = self._state[cid]
            alpha = attack if target_b >= prev_b else decay
            new_b = prev_b + (target_b - prev_b) * alpha
            blended = (
                prev_color[0] + (target_color[0] - prev_color[0]) * colour_lerp,
                prev_color[1] + (target_color[1] - prev_color[1]) * colour_lerp,
                prev_color[2] + (target_color[2] - prev_color[2]) * colour_lerp,
            )
            m = max(blended)
            nc = (blended[0] / m, blended[1] / m, blended[2] / m) if m > 1e-6 else (0.0, 0.0, 0.0)
            self._state[cid] = (nc, new_b)
            # Soften colours toward white per the mode (keeps max channel = 1).
            sat = p.colour_sat
            if sat < 1.0:
                nc = (nc[0] * sat + (1.0 - sat), nc[1] * sat + (1.0 - sat), nc[2] * sat + (1.0 - sat))
            # Continuous brightness + sharp flash, then master-brightness scaling.
            b = min(1.0, new_b + self._flash) * self.brightness
            out[cid] = (nc[0] * b, nc[1] * b, nc[2] * b)
        return out
