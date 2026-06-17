"""The choreography engine: features + palette + mode params -> per-channel RGB.

Owns per-channel smoothing for natural motion (fast attack on beats, gentle
decay), advances a colour-drift phase over time, and renders via the single
parametric :func:`.modes.render` driven by the active mode. Output is a
``{channel_id: (r, g, b)}`` map of 0..1 colours for the xy+brightness encoder.
"""

from __future__ import annotations

import math
from collections import deque

from ..audio.analyzer import AnalysisFrame
from ..audio.structure import StructureState
from ..audio.tempo import BeatGrid
from ..color.palette import RGB, Palette, get_palette
from ..const import (
    DEFAULT_EFFECT,
    DEFAULT_MODE,
    MELBANK_BINS,
    ColorScheme,
    SyncEffect,
    SyncMode,
)
from ..hue.bridge import EntertainmentChannel
from .fireworks import FireworksEffect
from .modes import (
    MODE_PARAMS,
    MOVIE_PARAMS,
    ROLE_BASS,
    ROLE_MID,
    ROLE_VOCAL,
    accent_knee,
    assign_roles,
    band_for_rank,
    beat_colour_advance,
    beat_pulse,
    kick_flash,
    mid_flash,
    pulse_weight,
    render,
)
from .spatial import (
    Wave,
    distance,
    floor_origin,
    height_band,
    melbank_window,
    normalize_positions,
)

_FLASH_DECAY = 0.80  # default per-frame fade of the beat flash (modes override)

# Asymmetric band envelope followers (LedFx-style): rise fast on new energy,
# fall gently, so continuous brightness rides the music instead of jittering.
_ENV_RISE = 0.55
_ENV_FALL = 0.10

# Slow per-bin melbank baseline (the reference the spectral transient rises over):
# rises gently, falls slowly, so a sustained tone is absorbed into the baseline
# and only fresh attacks register as a transient pop.
_MEL_SLOW_RISE = 0.25
_MEL_SLOW_FALL = 0.06

# Locked mid (guitar/snare) pops are quantised to the eighth-note grid: hits
# within this phase distance of an eighth slot (on-beat or off-beat) pass,
# syllables and ornaments floating between slots do not.
_EIGHTH_PHASE = 0.12

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
        self._swell = 0.0  # structural drop swell (decays)
        # Smoothed track-section level (1.0 when unknown): quiet sections pull
        # the show in (dimmer base, tighter colour span, softer waves) so the
        # chorus visibly *arrives*. Eased over ~2 s so changes feel musical.
        self.section_level = 1.0
        # Instrument-role state: which light is "the bass", "the guitar", "the
        # vocal" right now. Rotated every few bars and on drops so the band
        # members trade places around the room.
        self.roles: dict[int, int] = {}
        self._role_offset = 0
        self._beats_seen = 0
        self._waves: list[Wave] = []  # live beat wavefronts
        self._wave_armed = True  # PLL anticipation: ready to fire the next wave
        self._fireworks = FireworksEffect()
        self.set_channels(channels)

    def set_channels(self, channels: list[EntertainmentChannel]) -> None:
        self.channels = channels
        order = sorted(channels, key=lambda c: c.x)
        self._rank_ids = [c.channel_id for c in order]
        n = len(order)
        positions = normalize_positions(channels)  # (nx, ny, nz) in 0..1
        self._origin = floor_origin(positions)
        self.cmap: dict[int, dict] = {}
        for rank, ch in enumerate(order):
            nx, ny, nz = positions[ch.channel_id]
            xrank = rank / max(1, n - 1)
            # Spread the melbank across the room: leftmost lamp rides the lowest
            # frequencies, rightmost the highest (LedFx "Wavelength"). Each lamp
            # averages an overlapping window so the field stays smooth.
            mel_lo, mel_hi = melbank_window(xrank, MELBANK_BINS)
            self.cmap[ch.channel_id] = {
                "norm_x": (ch.x + 1.0) / 2.0,
                "xrank": xrank,
                "band": band_for_rank(rank, n),
                "nx": nx,
                "ny": ny,
                "nz": nz,
                "hband": height_band(nz),  # frequency band by lamp height
                "dist_origin": distance((nx, ny, nz), self._origin),
                "mel_lo": mel_lo,
                "mel_hi": mel_hi,
            }
        self._waves = []
        self._wave_armed = True
        # Rolling accents of recent beats: the highlight ranking context.
        self._accents: deque[float] = deque(maxlen=24)
        self._state: dict[int, tuple[RGB, float]] = {
            ch.channel_id: ((0.0, 0.0, 0.0), 0.0) for ch in channels
        }
        self._env: dict[str, float] = {}
        # Smoothed room loudness (asymmetric follower): the whole room brightens
        # and dims with the song's energy contour. Drives the unified modes.
        self._energy_env: float = 0.0
        # Per-bin melbank baseline + transient: the slow EMA tracks each band's
        # sustained level, and the transient is how far the band has jumped above
        # it right now. Every lamp pops on transients in *its* slice of the
        # spectrum, so any instrument in any frequency range drives the lights.
        self._mel_slow: list[float] = []
        self._mel_transient: list[float] = []
        self._light_flash: dict[int, float] = {}
        self.roles = {}
        self._role_offset = 0
        self._beats_seen = 0

    @property
    def band_env(self) -> dict[str, float]:
        """Asymmetric-follower band envelopes (read by :func:`.modes.render`)."""
        return self._env

    def _update_env(self, frame: AnalysisFrame) -> None:
        for name, value in frame.bands.items():
            prev = self._env.get(name, 0.0)
            alpha = _ENV_RISE if value > prev else _ENV_FALL
            self._env[name] = prev + (value - prev) * alpha
        # Room loudness contour: snap up on a swell, ease down through quieter
        # passages, so the whole room follows the rhythm of the song.
        a = _ENV_RISE if frame.energy > self._energy_env else _ENV_FALL
        self._energy_env += (frame.energy - self._energy_env) * a

    @property
    def energy_env(self) -> float:
        """Smoothed room loudness 0..1 (read by :func:`.modes.render`)."""
        return self._energy_env

    @property
    def mel_transient(self) -> list[float]:
        """Per-bin melbank transient (rise above the slow baseline, 0..1)."""
        return self._mel_transient

    def _update_mel_transient(self, frame: AnalysisFrame) -> None:
        """Track each melbank bin's slow baseline and its transient over it.

        The transient is what makes the room react to *every* instrument: a kick
        spikes the low bins, a snare the low-mids, a guitar the mids, a cymbal
        the highs - each lamp then pops on the transient in its own spectral
        slice. It rides above the slow baseline so a sustained tone fades from
        the pop (only the attack reads), keeping the club bright<->dark snap.
        """
        mel = frame.melbank
        if not mel:
            self._mel_transient = []
            return
        if len(self._mel_slow) != len(mel):
            self._mel_slow = list(mel)
        tr: list[float] = []
        slow = self._mel_slow
        for i, v in enumerate(mel):
            s = slow[i]
            s += (v - s) * (_MEL_SLOW_RISE if v > s else _MEL_SLOW_FALL)
            slow[i] = s
            tr.append(v - s if v > s else 0.0)
        self._mel_transient = tr

    @property
    def role_offset(self) -> int:
        """Rotation counter of the instrument-role layout (read by modes)."""
        return self._role_offset

    def _update_roles(self, p, frame: AnalysisFrame, beatgrid, structure) -> None:
        """Refresh which light plays which instrument; rotate musically.

        Rotation advances every ``role_rotate_beats`` musical beats (grid ticks
        when locked, kicks otherwise) and reshuffles instantly on a drop, so
        the band members keep trading places around the room.
        """
        ticked = (
            beatgrid.predicted_beat
            if (beatgrid is not None and beatgrid.locked)
            else frame.bass_beat
        )
        if ticked and p.role_rotate_beats > 0:
            self._beats_seen += 1
            if self._beats_seen >= p.role_rotate_beats:
                self._beats_seen = 0
                self._role_offset += 1
        if structure is not None and structure.drop_now:
            self._role_offset += 1  # a drop reshuffles the band
        role_list = assign_roles(len(self._rank_ids), p.role_mix, self._role_offset)
        self.roles = dict(zip(self._rank_ids, role_list))

    def _beat_highlight(self, p, accent: float, append: bool) -> bool:
        """Is a beat with this accent a *highlight* of the current passage?

        Rank-based selectivity (the apartment-sync look): the beat qualifies
        when its accent reaches the mode's quantile of the recent beats'
        accents. Ranking adapts where a fixed threshold goes blind — in a flat
        four-to-the-floor passage every kick IS the pulse and all qualify,
        while in a dynamic mix only the hits a listener would pick out do.
        ``append`` folds the accent into the window (exactly once per beat);
        anticipatory callers (waves) peek without appending. The beat is
        ranked against the window *before* it joins it — counting itself
        would let a borderline beat drag the threshold down to its own level.
        """
        win = self._accents
        if p.highlight_quantile <= 0.0:
            ok = True
        elif len(win) < 8:  # not enough context yet: fall back to the floor
            ok = accent >= p.accent_floor
        else:
            ranked = sorted(win)
            thr = ranked[min(len(ranked) - 1, int(p.highlight_quantile * len(ranked)))]
            ok = accent >= thr
        if append:
            win.append(accent)
        return ok

    @staticmethod
    def _visible_event(
        frame: AnalysisFrame, beatgrid: BeatGrid | None
    ) -> tuple[float, float, bool]:
        """(strength, bass, grid_locked) of the beat driving visible accents.

        **Reactive first.** A detected bass onset (``frame.bass_beat``) ALWAYS
        fires — this is what makes the room actually move with the song (it is
        exactly what the Fireworks effect reacts to). When a tempo grid is
        locked it *also* fires the scheduled/anticipated beat (so beats land
        even through a dense bar), and the larger of the two wins. The grid is
        an enhancement, never a gate: previously the locked path required a
        detected onset to sit within a tight phase window of the grid, so a
        slightly-misaligned grid (common on a replayed track map) rejected the
        real beats and the show went dead while the audio was clearly pumping.
        """
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
        det = frame.bass_strength if frame.bass_beat else 0.0
        if beatgrid is not None and beatgrid.locked:
            sched = 0.0
            if beatgrid.predicted_beat:
                acc = max(0.0, min(1.0, beatgrid.accent_now))
                sched = 1.0 + 2.0 * acc
            strength = max(sched, det)
            return (strength, bass, True) if strength > 0.0 else (0.0, 0.0, True)
        return (det, bass, False) if det > 0.0 else (0.0, 0.0, False)

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

        When locked we fire ``anticipation_ms`` *before* the predicted beat so
        the wave reaches the room as the kick lands (cancelling bulb latency),
        and the wave is **sized by the upcoming beat's accent** (known from the
        track map) — main beats roll big, ordinary beats ripple. When unlocked
        we fall back to firing on bass onsets, accent-knee scaled, so an
        uncertain grid degrades to smaller, surer waves rather than one per
        hi-hat.
        """
        antic = p.anticipation_ms / 1000.0
        strength = 0.0
        if beatgrid is not None and beatgrid.locked:
            if beatgrid.predicted_beat:
                self._wave_armed = True  # re-arm for the next beat
            if self._wave_armed and beatgrid.time_to_next_beat <= antic:
                self._wave_armed = False
                # Sized by the upcoming beat's accent AND its bar position,
                # through the same pulse shaping AND the same highlight ranking
                # as the flashes (peeked, not appended — the at-beat path owns
                # the window) — selective modes only roll waves for the beats
                # that matter; weak ticks barely ripple.
                acc = max(0.0, min(1.0, beatgrid.accent))
                nb = (beatgrid.beat_in_bar + 1) % 4
                hl = self._beat_highlight(p, acc, append=False)
                w = pulse_weight(p, acc, nb, hl)
                if w > 0.0:
                    strength = (
                        (0.45 + 1.05 * acc)
                        * (0.15 + 0.85 * w)
                        * beatgrid.schedule_strength
                    )
        elif vis_strength > 0.0:
            knee = accent_knee(vis_strength, p.beat_threshold)
            if knee > 0.2:
                strength = min(1.5, (0.5 + vis_strength) * knee)
        if strength <= 0.0:
            return
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
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
        self._update_mel_transient(frame)
        # Refresh the instrument-role assignments (rotate on schedule + drops).
        self._update_roles(p, frame, beatgrid, structure)
        # One decision for everything visible: the scheduled beat (locked) or
        # the qualifying kick (unlocked reactive fallback).
        vis_strength, vis_bass, grid_locked = self._visible_event(frame, beatgrid)
        # The highlight decision: rank this beat's accent against the recent
        # passage. Selective modes only *fire* on highlights — everything else
        # ticks quietly or stays dark — which is what makes the hits read as
        # choreography instead of a metronome. ``beat_now`` marks the frame the
        # beat actually starts (detection frames that merely enlarge a pulse
        # must not re-append to the ranking window or re-jump the colour).
        acc_now = 0.0
        highlight = False
        beat_now = False
        if vis_strength > 0.0:
            acc_now = max(0.0, min(1.0, (vis_strength - 1.0) / 2.0))
            # A real beat this frame: a detected onset, or (when locked) the
            # scheduled beat. Either advances the colour and the highlight
            # window - so colour jumps WITH the song's beats, not on a timer.
            beat_now = (
                frame.bass_beat
                or (not grid_locked)
                or (beatgrid is not None and beatgrid.predicted_beat)
            )
            highlight = self._beat_highlight(p, acc_now, append=beat_now)
        # Mid (guitar/snare) onsets from their own dedicated detector stream.
        # They only ever reach the mid-role lights, so they read as "that light
        # is the guitar". When the grid is locked they're quantised to the
        # eighth-note slots: syncopated riffs live on that grid, vocal
        # syllables float between its slots — the other half of the old
        # "pops on the singing" problem.
        mid_strength = frame.mid_strength if frame.mid_beat else 0.0
        if mid_strength > 0.0 and grid_locked and beatgrid is not None:
            ph = beatgrid.phase % 0.5
            if min(ph, 0.5 - ph) > _EIGHTH_PHASE:
                mid_strength = 0.0
        # Advance the palette position. Colour-jump modes make colour the
        # PRIMARY motion (the apartment-sync look): the whole room snaps to a
        # new palette position on EVERY beat — a big spectrum-spanning jump —
        # with highlights jumping further still, so the colour reads as the
        # beat. Between beats it holds (only a slow drift). Legacy modes keep
        # the fluid continuous roll (the Hue+Spotify look).
        self.colour_phase += p.colour_speed * dt
        # Continuous, loudness-scaled colour drift (LedFx-style): colour keeps
        # moving with the music between beats, so the show never freezes when no
        # beat is detected or the grid is unlocked. Beat colour-jumps add on top.
        if p.colour_flow > 0.0:
            self.colour_phase += p.colour_flow * (0.25 + 0.75 * frame.energy) * dt
        sect = 0.6 + 0.4 * self.section_level
        rolling = (
            beatgrid is not None
            and beatgrid.locked
            and beatgrid.period_s > 0.05
            and p.colour_beat_step > 0.0
        )
        if p.colour_jump > 0.0:
            if beat_now:
                # Every beat jumps; highlights jump ~1.7x so the standout hits
                # land the biggest hue change.
                step = p.colour_jump * (0.55 + 0.45 * acc_now)
                if highlight:
                    step *= 1.7
                self.colour_phase += step * sect
            if rolling:  # a whisper of tempo-locked roll underneath the jumps
                self.colour_phase += (
                    p.colour_beat_step * 0.2 * (dt / beatgrid.period_s) * sect
                )
        else:
            adv = beat_colour_advance(p, vis_strength, vis_bass) * sect
            if rolling:
                self.colour_phase += (
                    p.colour_beat_step * 0.7 * (dt / beatgrid.period_s) * sect
                )
                self.colour_phase += adv * 0.35
            else:
                self.colour_phase += adv

        # Spatial beat wavefronts (predictive when a beat grid is supplied).
        if p.wave_gain > 0.0:
            self._spawn_waves(p, frame, beatgrid, vis_strength)
            for w in self._waves:
                w.advance(dt, decay_tau=0.45)
            self._waves = [w for w in self._waves if not w.dead()]

        # Per-light beat snaps: bass-role lights snap on kicks, mid-role lights
        # pop on guitar hits. In the wavefront-led classic modes the synchronous
        # snap is scaled down (the wave carries the beat); hard-snap modes slam
        # on top of the wave — that is the point of them. Grid-locked pulses
        # are shaped by accent + bar position + the highlight ranking
        # (highlights slam, ordinary beats tick at weak_pulse or stay dark);
        # the binary accent knee only survives in the unlocked reactive
        # fallback where there is no schedule to rank against.
        flash_scale = 1.0 if p.hard_snap else max(0.0, 1.0 - p.wave_gain)
        if grid_locked and beatgrid is not None:
            kick = 0.0
            if vis_strength > 0.0:
                kick = (
                    beat_pulse(p, acc_now, beatgrid.beat_in_bar, vis_bass, highlight)
                    * flash_scale
                    * beatgrid.schedule_strength
                )
        else:
            kick = kick_flash(p, vis_strength, vis_bass) * flash_scale
        midf = mid_flash(p, mid_strength)
        # The full-room moment: the passage's very biggest hits take EVERY
        # light at once (vocal lights a touch softer), the way the reference
        # shows punctuate a chorus. Ordinary highlights stay role-separated.
        full_room = kick > 0.0 and highlight and acc_now >= p.full_room_accent
        lf = self._light_flash
        decay = p.flash_decay  # per-mode: lower = snappier firework fall
        for cid in lf:
            lf[cid] *= decay
        if kick > 0.0 or midf > 0.0:
            for cid, role in self.roles.items():
                if full_room:
                    f = kick * (0.85 if role == ROLE_VOCAL else 1.0)
                    if role == ROLE_MID:
                        f = max(f, midf)
                    lf[cid] = max(lf.get(cid, 0.0), f)
                elif role == ROLE_MID:
                    if midf > 0.0:
                        lf[cid] = max(lf.get(cid, 0.0), midf)
                elif kick > 0.0 and role == ROLE_BASS:
                    lf[cid] = max(lf.get(cid, 0.0), kick)

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
        out: dict[int, RGB] = {}
        for cid, (target_color, target_b) in targets.items():
            overlay = self._light_flash.get(cid, 0.0) + self._swell
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
