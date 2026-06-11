"""The choreography engine: features + palette + mode params -> per-channel RGB.

Owns per-channel smoothing for natural motion (fast attack on beats, gentle
decay), advances a colour-drift phase over time, and renders via the single
parametric :func:`.modes.render` driven by the active mode. Output is a
``{channel_id: (r, g, b)}`` map of 0..1 colours for the xy+brightness encoder.
"""

from __future__ import annotations

import math

from ..audio.analyzer import AnalysisFrame
from ..audio.structure import StructureState
from ..audio.tempo import BeatGrid
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
from .spatial import Wave, distance, floor_origin, height_band, normalize_positions

_FLASH_DECAY = 0.80  # per-frame fade of the beat flash overlay (~5 frames)

# Asymmetric band envelope followers (LedFx-style): rise fast on new energy,
# fall gently, so continuous brightness rides the music instead of jittering.
_ENV_RISE = 0.55
_ENV_FALL = 0.10

# When the tempo grid is locked, only onsets within this phase distance of a
# predicted beat may drive visible accents (flash/colour/waves); anything in
# the middle of the beat is a vocal hit or fill, not the pulse.
_ONGRID_PHASE = 0.18

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
        self._swell = 0.0  # structural drop swell (decays)
        # Smoothed track-section level (1.0 when unknown): quiet sections pull
        # the show in (dimmer base, tighter colour span, softer waves) so the
        # chorus visibly *arrives*. Eased over ~2 s so changes feel musical.
        self.section_level = 1.0
        self._waves: list[Wave] = []  # live beat wavefronts
        self._wave_armed = True  # PLL anticipation: ready to fire the next wave
        self._fireworks = FireworksEffect()
        self.set_channels(channels)

    def set_channels(self, channels: list[EntertainmentChannel]) -> None:
        self.channels = channels
        order = sorted(channels, key=lambda c: c.x)
        n = len(order)
        positions = normalize_positions(channels)  # (nx, ny, nz) in 0..1
        self._origin = floor_origin(positions)
        self.cmap: dict[int, dict] = {}
        for rank, ch in enumerate(order):
            nx, ny, nz = positions[ch.channel_id]
            self.cmap[ch.channel_id] = {
                "norm_x": (ch.x + 1.0) / 2.0,
                "xrank": rank / max(1, n - 1),
                "band": band_for_rank(rank, n),
                "nx": nx,
                "ny": ny,
                "nz": nz,
                "hband": height_band(nz),  # frequency band by lamp height
                "dist_origin": distance((nx, ny, nz), self._origin),
            }
        self._waves = []
        self._wave_armed = True
        self._state: dict[int, tuple[RGB, float]] = {
            ch.channel_id: ((0.0, 0.0, 0.0), 0.0) for ch in channels
        }
        self._env: dict[str, float] = {}

    @property
    def band_env(self) -> dict[str, float]:
        """Asymmetric-follower band envelopes (read by :func:`.modes.render`)."""
        return self._env

    def _update_env(self, frame: AnalysisFrame) -> None:
        for name, value in frame.bands.items():
            prev = self._env.get(name, 0.0)
            alpha = _ENV_RISE if value > prev else _ENV_FALL
            self._env[name] = prev + (value - prev) * alpha

    @staticmethod
    def _visible_event(frame: AnalysisFrame, beatgrid: BeatGrid | None) -> tuple[float, float]:
        """(strength, bass) of a beat allowed to drive visible accents, else (0, 0).

        Visible accents (flash, colour step, fallback waves) come from *bass*
        onsets only — vocals and hi-hats live above the bass band and are what
        made the show feel random. When the tempo grid is locked, the onset must
        also land near a predicted beat; off-grid kicks are syncopation, not the
        pulse, and the wavefronts already carry the grid.
        """
        if not frame.bass_beat:
            return 0.0, 0.0
        if beatgrid is not None and beatgrid.locked:
            phase = beatgrid.phase
            if _ONGRID_PHASE < phase < 1.0 - _ONGRID_PHASE:
                return 0.0, 0.0
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
        return frame.bass_strength, bass

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
            # Respect a dark theme swatch's value even in the idle glow.
            d = dim * (0.35 + 0.65 * m)
            out[ch.channel_id] = (nc[0] * d, nc[1] * d, nc[2] * d)
        return out

    @property
    def active_waves(self) -> list[Wave]:
        """Live beat wavefronts (read by :func:`.modes.render`)."""
        return self._waves

    def render(
        self,
        frame: AnalysisFrame,
        dt: float,
        beatgrid: BeatGrid | None = None,
        structure: StructureState | None = None,
    ) -> dict[int, RGB]:
        """Advance time and produce per-channel RGB (0..1) for the active effect.

        ``beatgrid`` (predicted tempo/phase) lets beats be *anticipated* so the
        light peaks on the kick; ``structure`` drives build/drop choreography.
        Both are optional — without them the engine renders purely reactively.
        """
        self.time += dt
        if self.effect is SyncEffect.FIREWORKS:
            # Fireworks owns its own per-light snap/fade, so it bypasses the music
            # smoothing pipeline; it still advances colour_phase for its ember glow.
            self.colour_phase += self.active_params.colour_speed * dt
            return self._fireworks.render(self, frame, dt)
        return self._render_music(frame, dt, beatgrid, structure)

    def _spawn_waves(
        self,
        p,
        frame: AnalysisFrame,
        beatgrid: BeatGrid | None,
        vis_strength: float,
    ) -> None:
        """Launch a beat wavefront, anticipating the beat when tempo is locked.

        When locked we fire ``anticipation_ms`` *before* the predicted beat so the
        wave reaches the room as the kick lands (cancelling bulb latency). When
        unlocked we fall back to firing on qualifying bass onsets only — the same
        ``beat_threshold`` gate as the flash, so an uncertain grid degrades to
        *fewer*, surer waves rather than one per hi-hat.
        """
        antic = p.anticipation_ms / 1000.0
        fire = False
        if beatgrid is not None and beatgrid.locked:
            if beatgrid.predicted_beat:
                self._wave_armed = True  # re-arm for the next beat
            if self._wave_armed and beatgrid.time_to_next_beat <= antic:
                fire = True
                self._wave_armed = False
        elif vis_strength >= p.beat_threshold:
            fire = True
        if not fire:
            return
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
        strength = min(1.5, 0.5 + vis_strength) if vis_strength > 0.0 else 1.0
        self._waves.append(
            Wave(
                origin=self._origin,
                strength=strength * (0.5 + 0.5 * bass),
                speed=p.wave_speed,
                width=p.wave_width,
            )
        )
        if len(self._waves) > 6:  # bound the live set
            self._waves.pop(0)

    def _render_music(
        self,
        frame: AnalysisFrame,
        dt: float,
        beatgrid: BeatGrid | None,
        structure: StructureState | None,
    ) -> dict[int, RGB]:
        """Smoothed beat/frequency/spatial choreography (Music and Movies).

        Colour (full-value chromaticity) and brightness are smoothed separately;
        the colour is renormalised to max-channel 1 so brightness is carried
        purely by ``new_b``. That keeps mode floors honest (the encoder reads
        brightness as the max channel) even mid colour-transition.
        """
        p = self.active_params
        self._update_env(frame)
        # One decision for everything visible: which onset (if any) qualifies.
        vis_strength, vis_bass = self._visible_event(frame, beatgrid)
        # Advance the palette position: a slow continuous drift plus a step on
        # every visible beat so the colour moves with the music (bolder steps
        # in loud sections, gentler in quiet ones).
        self.colour_phase += p.colour_speed * dt
        self.colour_phase += beat_colour_advance(p, vis_strength, vis_bass) * (
            0.6 + 0.4 * self.section_level
        )

        # Spatial beat wavefronts (predictive when a beat grid is supplied).
        if p.wave_gain > 0.0:
            self._spawn_waves(p, frame, beatgrid, vis_strength)
            for w in self._waves:
                w.advance(dt, decay_tau=0.45)
            self._waves = [w for w in self._waves if not w.dead()]

        # Beat-flash overlay: the wave carries most of the beat in spatial modes,
        # so the synchronous flash is scaled down (spatial distribution is both
        # nicer and safer than flashing every lamp together).
        flash_scale = max(0.0, 1.0 - p.wave_gain)
        self._flash = max(
            self._flash * _FLASH_DECAY,
            beat_flash(p, vis_strength, vis_bass) * flash_scale,
        )

        # Structure choreography: builds desaturate (tension), drops swell, and
        # the section arc (from the track map) scales the show's whole range.
        sat_mul = 1.0
        if structure is not None:
            sat_mul = 1.0 - p.build_desat * structure.build_progress
            drop = p.drop_boost if structure.drop_now else 0.0
            self._swell = max(self._swell * 0.85, drop)
            alpha = 1.0 - math.exp(-dt / 2.0)
            self.section_level += (structure.section_level - self.section_level) * alpha
        else:
            self._swell *= 0.85

        targets = render(self, frame)

        colour_lerp = p.colour_lerp
        attack, decay = p.bri_attack, p.bri_decay
        overlay = self._flash + self._swell
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
            # Soften colours toward white per the mode and the build tension.
            sat = p.colour_sat * sat_mul
            if sat < 1.0:
                nc = (nc[0] * sat + (1.0 - sat), nc[1] * sat + (1.0 - sat), nc[2] * sat + (1.0 - sat))
            # Cinematic warm drift: in quiet moments (Movie) pull the colour
            # toward a cosy tungsten white, easing back to the artwork hue as the
            # scene gets louder. Renormalise so brightness stays on its own track.
            if p.warm_calm:
                calm = p.warm_calm * (1.0 - min(1.0, frame.energy))
                if calm > 0.0:
                    nc = (
                        nc[0] * (1.0 - calm) + 1.00 * calm,
                        nc[1] * (1.0 - calm) + 0.82 * calm,
                        nc[2] * (1.0 - calm) + 0.62 * calm,
                    )
                    mx = max(nc) or 1.0
                    nc = (nc[0] / mx, nc[1] / mx, nc[2] / mx)
            # Continuous brightness + sharp flash/swell, then master scaling.
            b = min(1.0, new_b + overlay) * self.brightness
            out[cid] = (nc[0] * b, nc[1] * b, nc[2] * b)
        return out
