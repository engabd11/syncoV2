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
    _melbank_drive,
    _pan_weighted_mean,
    accent_knee,
    assign_roles,
    band_for_rank,
    beat_colour_advance,
    beat_pulse,
    event_gates,
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
    phrase_origins,
)

_FLASH_DECAY = 0.80  # default per-frame fade of the beat flash (modes override)

# Below this loudness the track is treated as silent: all beat reactions (flash,
# colour jump, waves) fade out and stop, so only real audio ever moves the
# lights (a scheduled/stray beat can't strobe a finished or paused track).
_SILENCE_GATE = 0.12
# The gate rides a fast-decaying peak-hold of the loudness so it stays high
# through the gaps between beats (a kick spikes it) but drops within ~0.2 s of
# the audio actually stopping — much snappier than the slow energy envelope,
# which is tuned for gentle breathing, not a silence cut-off.
_GATE_DECAY = 0.85
# Loudness at/above which a beat flashes at full; below it the flash scales DOWN
# in proportion to the beat's actual height, so a track fading out (tiny beats)
# brightens a little, not at full. Uses the smoothed energy contour (not the
# peak-hold gate, which holds through the fade), so it follows the fade down.
_LOUD_REF = 0.30

# Asymmetric band envelope followers (LedFx-style): rise fast on new energy,
# fall gently, so continuous brightness rides the music instead of jittering.
_ENV_RISE = 0.55
_ENV_FALL = 0.10

# Slow per-bin melbank baseline (the reference the spectral transient rises over):
# rises gently, falls slowly, so a sustained tone is absorbed into the baseline
# and only fresh attacks register as a transient pop.
_MEL_SLOW_RISE = 0.25
_MEL_SLOW_FALL = 0.06

# Per-band "presence" envelope (which instruments are actually playing right now):
# rises in ~0.5 s when a band comes alive, falls over ~3 s so a brief gap doesn't
# drop it. Drives dynamic role assignment so no lamp sits on a dead band.
_PRESENCE_RISE = 0.04
_PRESENCE_FALL = 0.008

# Locked mid (guitar/snare) pops are quantised to the eighth-note grid: hits
# within this phase distance of an eighth slot (on-beat or off-beat) pass,
# syllables and ornaments floating between slots do not.
_EIGHTH_PHASE = 0.12

# Rhythm confidence: evidence the song currently HAS an actual beat, gating the
# unlocked (detected-onset) flash path in the permissive club modes (see
# ModeParams.nobeat_flash). A locked tempo grid is proof — dense mixes with
# buried kicks still lock — so it rises fast while locked; unlocked, only
# clearly broadband onsets (real kicks, per the width calibration in
# MODE_PARAMS) count, one jump per event since onsets are single frames. It
# decays over a few seconds so a beat-less passage softens the flashes to the
# mode's floor instead of strobing a dark room, and a returning beat recovers
# within a couple of real kicks.
_RHYTHM_RISE = 0.10        # per-frame rise toward 1 while locked (~0.5 s)
_RHYTHM_EVENT_RISE = 0.35  # rise per qualifying unlocked kick (sparse events)
_RHYTHM_FALL = 0.006       # per-frame decay (half-life ~2.3 s at 50 fps)
# Onset-width span for unlocked kick evidence: sung vowels measure ~0.11-0.13,
# an isolated sine kick ~0.49 (see the width ladder in modes.MODE_PARAMS).
_RHYTHM_W_LO = 0.13
_RHYTHM_W_HI = 0.40

# Pre-drop anticipation (ModeParams.predrop_depth): the room pulls in during
# the final moments before a drop, then detonates out of the held-back
# tension. A *scheduled* drop (track-map section boundary, drop_eta_s known)
# is trusted immediately and the envelope deepens as the ETA counts down over
# _PREDROP_RAMP_S. The *heuristic* flag (a build that nearly maxed out) is
# treated with suspicion: it must persist _PREDROP_CONFIRM frames to commit,
# is capped at _PREDROP_HEUR_CAP depth, eases out if no drop lands within
# _PREDROP_TIMEOUT_S, and then blocks re-commits for _PREDROP_REFRACTORY_S —
# a flickering build detector can never repeatedly dim the room.
_PREDROP_RAMP_S = 1.2      # scheduled: full depth this many seconds before the boundary
_PREDROP_CONFIRM = 10      # heuristic: consecutive frames (~200 ms) before committing
_PREDROP_HEUR_CAP = 0.6    # heuristic: max envelope (the map path may reach 1.0)
_PREDROP_TIMEOUT_S = 3.0   # commit with no drop: ease back out after this long
_PREDROP_REFRACTORY_S = 8.0  # and ignore the heuristic flag for this long after
_PREDROP_RISE = 0.10       # max envelope rise per frame (~0.2 s to full)
_PREDROP_FALL = 0.04       # ease-out rate on timeout (drops snap to 0 instead)

# Phrase-level colour-jump variation (ModeParams.phrase_bars): each phrase the
# per-beat jump magnitude cycles through these multipliers — subtle, seed-free
# and repeatable, so long steady sections evolve without feeling random.
_PHRASE_JUMP = (1.0, 0.85, 1.15, 0.95)

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
        # Drum-pad mode: when set, the automatic beat reactions (flashes, waves,
        # beat colour-jumps) are suppressed so the user's taps drive the beats;
        # the continuous colour/energy ambience keeps running underneath.
        self.manual_only = False
        self._fireworks = FireworksEffect()
        self.set_channels(channels)

    def set_channels(self, channels: list[EntertainmentChannel]) -> None:
        self.channels = channels
        order = sorted(channels, key=lambda c: c.x)
        self._rank_ids = [c.channel_id for c in order]
        n = len(order)
        positions = normalize_positions(channels)  # (nx, ny, nz) in 0..1
        self._origin = floor_origin(positions)
        # Phrase-cycled wave origins (centre/left/right/centre) with the
        # per-channel distances precomputed for each.
        self._origins = phrase_origins(positions)
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
                "dist_origins": [
                    distance((nx, ny, nz), o) for o in self._origins
                ],
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
        # Per-band presence (slow envelope): which instruments are playing right
        # now, used to assign dynamic roles only to bands that actually have
        # content so no lamp is stuck on a dead "guitar"/"vocal" role.
        self._presence: dict[str, float] = {}
        self._role_mix_eff: tuple[float, float, float] | None = None
        # Smoothed room loudness (asymmetric follower): the whole room brightens
        # and dims with the song's energy contour. Drives the unified modes.
        self._energy_env: float = 0.0
        # Fast-decaying loudness peak-hold for the silence gate (see _GATE_DECAY).
        self._loud: float = 0.0
        # Rhythm confidence 0..1 (see the _RHYTHM_* constants): does the song
        # currently have an actual beat? Gates the unlocked flash path.
        self._rhythm_conf: float = 0.0
        # Pre-drop pull-down state (see the _PREDROP_* constants).
        self._predrop = 0.0          # rendered envelope 0..1
        self._predrop_commit = False
        self._predrop_streak = 0     # heuristic confirm counter
        self._predrop_commit_t = 0.0
        self._predrop_block_until = 0.0
        # Depth held at the moment a drop landed (consumed the same frame by
        # the drop swell: a deeper pull-down earns a bigger detonation).
        self.predrop_released = 0.0
        # Per-bin melbank baseline + transient: the slow EMA tracks each band's
        # sustained level, and the transient is how far the band has jumped above
        # it right now. Every lamp pops on transients in *its* slice of the
        # spectrum, so any instrument in any frequency range drives the lights.
        self._mel_slow: list[float] = []
        self._mel_transient: list[float] = []
        self._light_flash: dict[int, float] = {}
        # Last emitted per-light brightness, for the rise slew-rate limiter that
        # turns a beat into a fast SWING instead of a 1-frame strobe (bri_slew).
        self._emit_b: dict[int, float] = {}
        self.roles = {}
        self._role_offset = 0
        self._beats_seen = 0
        # Phrase-level evolution: locked downbeats advance the bar count; every
        # phrase_bars bars the phrase index cycles the wave origin and the
        # colour-jump magnitude (see _update_phrase).
        self._bar_count = 0
        self._phrase = 0

    @property
    def band_env(self) -> dict[str, float]:
        """Asymmetric-follower band envelopes (read by :func:`.modes.render`)."""
        return self._env

    def _update_env(self, frame: AnalysisFrame) -> None:
        for name, value in frame.bands.items():
            prev = self._env.get(name, 0.0)
            alpha = _ENV_RISE if value > prev else _ENV_FALL
            self._env[name] = prev + (value - prev) * alpha
            # Slow presence envelope: fast up, slow down (a band stays "present"
            # through brief gaps so roles don't flicker on every rest).
            pp = self._presence.get(name, 0.0)
            pa = _PRESENCE_RISE if value > pp else _PRESENCE_FALL
            self._presence[name] = pp + (value - pp) * pa
        # Room loudness contour: snap up on a swell, ease down through quieter
        # passages, so the whole room follows the rhythm of the song.
        a = _ENV_RISE if frame.energy > self._energy_env else _ENV_FALL
        self._energy_env += (frame.energy - self._energy_env) * a
        # Fast peak-hold for the silence gate.
        self._loud = max(frame.energy, self._loud * _GATE_DECAY)

    def _update_rhythm_conf(self, frame: AnalysisFrame, beatgrid) -> None:
        """Track evidence that the song currently has an actual beat.

        A locked tempo grid is proof (dense mixes with buried kicks still
        lock); while unlocked, only clearly broadband onsets — real kicks, per
        the onset-width calibration — count, so sung vowels and tonal swells
        that slip past the permissive club-mode width gates can never build
        confidence. See the _RHYTHM_* constants for the dynamics.
        """
        c = self._rhythm_conf
        if beatgrid is not None and beatgrid.locked:
            c += (1.0 - c) * _RHYTHM_RISE
        elif frame.bass_beat:
            ev = (frame.onset_width - _RHYTHM_W_LO) / (_RHYTHM_W_HI - _RHYTHM_W_LO)
            ev = max(0.0, min(1.0, ev))
            if ev > c:
                c += (ev - c) * _RHYTHM_EVENT_RISE
        self._rhythm_conf = c * (1.0 - _RHYTHM_FALL)

    def _update_predrop(self, p, structure) -> float:
        """Advance the pre-drop pull-down envelope; return its 0..1 value.

        See the _PREDROP_* constants for the full contract. On ``drop_now``
        the envelope snaps to zero in ONE frame — the sudden release out of
        the held-back dim is the detonation's contrast — and the depth it
        held is published via ``predrop_released`` so the drop swell can size
        with it.
        """
        self.predrop_released = 0.0
        if structure is None or p.predrop_depth <= 0.0:
            self._predrop = 0.0
            self._predrop_commit = False
            self._predrop_streak = 0
            return 0.0

        if structure.drop_now:
            if self._predrop > 0.05:
                self.predrop_released = self._predrop
            self._predrop = 0.0
            self._predrop_commit = False
            self._predrop_streak = 0
            return 0.0

        scheduled = structure.drop_eta_s >= 0.0
        if scheduled:
            # The map knows exactly when the boundary lands: trust it and
            # deepen as the ETA counts down (keeping commit_t fresh so the
            # timeout only fires if the ETA vanishes, e.g. a position jump).
            self._predrop_commit = True
            self._predrop_commit_t = self.time
            target = max(0.0, min(1.0, 1.0 - structure.drop_eta_s / _PREDROP_RAMP_S))
        elif self._predrop_commit:
            # Heuristic commit rides until the drop or the timeout.
            if self.time - self._predrop_commit_t > _PREDROP_TIMEOUT_S:
                self._predrop_commit = False
                self._predrop_streak = 0
                self._predrop_block_until = self.time + _PREDROP_REFRACTORY_S
                target = 0.0
            else:
                target = _PREDROP_HEUR_CAP
        elif structure.drop_imminent and self.time >= self._predrop_block_until:
            self._predrop_streak += 1
            if self._predrop_streak >= _PREDROP_CONFIRM:
                self._predrop_commit = True
                self._predrop_commit_t = self.time
            target = 0.0  # nothing applied until the flag proves persistent
        else:
            self._predrop_streak = 0
            target = 0.0

        env = self._predrop
        if target > env:
            env = min(target, env + _PREDROP_RISE)
        else:
            env = max(target, env - _PREDROP_FALL)
        self._predrop = env
        return env

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

    def _effective_mix(self, p) -> tuple[float, float, float]:
        """The (bass, mid, vocal) split to use right now.

        Static modes use the mode's fixed ``role_mix``. ``dynamic_roles`` modes
        weight it by each band's *presence* so lamps are dealt to the
        instruments actually playing — a track with no guitar gives its mid
        lamps to the bass/vocal that ARE there instead of leaving them dull, and
        the split re-deals as the song's instrumentation changes (item 6). Bass
        keeps a high floor (there is almost always low end); mid/vocal must be
        earned by real content.
        """
        if not p.dynamic_roles:
            return p.role_mix
        pr = self._presence
        pb = max(pr.get("sub_bass", 0.0), pr.get("bass", 0.0))
        pm = max(pr.get("low_mid", 0.0), pr.get("mid", 0.0))
        pv = max(pr.get("high", 0.0), 0.6 * pr.get("low_mid", 0.0))
        # Bass keeps a floor (low end is almost always there and anchors the
        # room); mid/vocal get weight ONLY in proportion to real presence, so an
        # absent instrument drops to zero and its lamps go to bands that are
        # playing instead of sitting dull. Presence is slow and the mix is only
        # re-evaluated on rotation, so this never flickers.
        wb = p.role_mix[0] * (0.5 + pb)
        wm = p.role_mix[1] * pm
        wv = p.role_mix[2] * pv
        s = wb + wm + wv
        if s <= 1e-6:
            return p.role_mix
        return (wb / s, wm / s, wv / s)

    def _update_roles(self, p, frame: AnalysisFrame, beatgrid, structure) -> None:
        """Refresh which light plays which instrument; rotate musically.

        Rotation advances every ``role_rotate_beats`` musical beats (grid ticks
        when locked, kicks otherwise) and reshuffles instantly on a drop, so
        the band members keep trading places around the room. The presence-
        weighted mix (dynamic modes) is re-evaluated on each rotation, so the
        instrument split also follows what's currently playing.
        """
        ticked = (
            beatgrid.predicted_beat
            if (beatgrid is not None and beatgrid.locked)
            else frame.bass_beat
        )
        rotated = False
        if ticked and p.role_rotate_beats > 0:
            self._beats_seen += 1
            if self._beats_seen >= p.role_rotate_beats:
                self._beats_seen = 0
                self._role_offset += 1
                rotated = True
        if structure is not None and structure.drop_now:
            self._role_offset += 1  # a drop reshuffles the band
            rotated = True
        # Recompute the (presence-weighted) mix on rotations and the first frame;
        # holding it steady between rotations keeps roles from flickering.
        if self._role_mix_eff is None or rotated:
            self._role_mix_eff = self._effective_mix(p)
        role_list = assign_roles(len(self._rank_ids), self._role_mix_eff, self._role_offset)
        self.roles = dict(zip(self._rank_ids, role_list))

    def _update_phrase(self, p, beatgrid, music_gate: float) -> None:
        """Advance the bar/phrase counters on locked downbeats.

        Counting simply pauses while the grid is unlocked, so a bad lock can
        never churn the phrase. Each completed phrase nudges the palette
        forward (a deterministic phrase marker) — the wave origin and the
        colour-jump multiplier read the phrase index directly.
        """
        if p.phrase_bars <= 0 or beatgrid is None or not beatgrid.locked:
            return
        if not (beatgrid.predicted_beat and beatgrid.beat_in_bar == 0):
            return
        self._bar_count += 1
        if self._bar_count % p.phrase_bars == 0:
            self._phrase += 1
            self.colour_phase += p.phrase_colour_shift * music_gate

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
        frame: AnalysisFrame, beatgrid: BeatGrid | None, det_gate: float = 1.0
    ) -> tuple[float, float, bool]:
        """(strength, bass, grid_locked) of the beat driving visible accents.

        **Reactive first.** A detected bass onset (``frame.bass_beat``) fires —
        this is what makes the room actually move with the song (it is
        exactly what the Fireworks effect reacts to). When a tempo grid is
        locked it *also* fires the scheduled/anticipated beat (so beats land
        even through a dense bar), and the larger of the two wins. The grid is
        an enhancement, never a gate: previously the locked path required a
        detected onset to sit within a tight phase window of the grid, so a
        slightly-misaligned grid (common on a replayed track map) rejected the
        real beats and the show went dead while the audio was clearly pumping.

        ``det_gate`` (the mode's onset-width gate) attenuates only the
        *detected* path: a narrowband onset (a sung vowel, a swelling tone)
        that slipped past the analyzer's shape guards is muted here, while
        scheduled grid beats — verified by the tempo model, not by this
        frame's spectrum — pass untouched (the flux-gated modes instead reality-
        check every beat by onset flux in EffectEngine._render_music).
        """
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
        det = frame.bass_strength * det_gate if frame.bass_beat else 0.0
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

    # -- drum-pad / manual beats ----------------------------------------------

    def set_manual_only(self, on: bool) -> None:
        """Suppress (or restore) the automatic beat reactions for drum-pad mode."""
        self.manual_only = bool(on)

    def _group_channels(self, group: str) -> list[int]:
        """Channels for a drum pad: thirds of the left→right spectral order.

        ``low``/``mid``/``high`` map to the left/centre/right third of the lamps
        (the same low→high spectral axis the room mirror shows). ``all`` — or any
        room with fewer than three lamps, where thirds don't divide cleanly — maps
        to every lamp so a tap is never a no-op.
        """
        ids = self._rank_ids
        n = len(ids)
        if n == 0:
            return []
        if n < 3 or group == "all":
            return list(ids)
        a, b = round(n / 3), round(2 * n / 3)
        if group == "low":
            return ids[:a]
        if group == "high":
            return ids[b:]
        return ids[a:b]  # "mid" (and any unknown group) -> the centre third

    def manual_flash(self, group: str, strength: float = 0.95) -> None:
        """Fire a one-shot beat flash on a pad's light group (a user tap).

        Writes the same per-light overlay the automatic beats use, so it rides
        the existing decay + brightness path; a small colour step makes the hue
        move with each hit too. Only meaningful while the render loop runs (music
        playing); on an idle frame there is nothing to apply it to.
        """
        s = max(0.0, min(1.0, strength))
        for cid in self._group_channels(group):
            self._light_flash[cid] = max(self._light_flash.get(cid, 0.0), s)
        self.colour_phase += 0.12  # the room's colour also moves on your hit

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
        if self.active_params.graph_reactive:
            # Extreme: a clean, direct "the song is a graph" renderer that ignores
            # the beat grid entirely (so no phantom/predicted beats) and reacts to
            # the actual spectrum — see _render_extreme.
            return self._render_extreme(frame, dt)
        return self._render_music(frame, dt, beatgrid, structure)

    def _render_extreme(self, frame: AnalysisFrame, dt: float) -> dict[int, RGB]:
        """Extreme, rebuilt from scratch: **the song is a graph** and each lamp
        reflects its own slice of that graph.

        The lamps are spread left→right across the audio spectrum (LedFx
        "Wavelength"): the leftmost rides the lowest frequencies, the rightmost
        the highest, so instruments naturally SEPARATE in space — a kick lights
        the low lamps, a snare/guitar the mids, a cymbal the highs. Stereo pan
        pulls each lamp's slice toward its side of the room, so a panned part
        lights the matching side (the 3D/spatial distribution).

        Each lamp's brightness is two proportional parts:

        * a smooth GLOW = that band's loudness (how high the graph sits there),
          so the room tracks the music going louder/quieter, and
        * a PEAK FLASH = a fresh transient in that band (how far the graph just
          jumped up), sized in direct proportion — a big peak flashes bright, a
          small one dim — added on top of the smoothed glow so it stays sharp.

        There is NO beat grid, no scheduled/predicted beat, no onset gating: a
        held tone or vocal has no fresh transient so it only glows (never
        strobes), a song tail simply fades as its loudness does, and every real
        hit — at any tempo — flashes exactly as big as it actually is.
        """
        p = self.active_params
        self._update_env(frame)
        self._update_mel_transient(frame)
        music_gate = min(1.0, self._loud / _SILENCE_GATE) if _SILENCE_GATE > 0 else 1.0
        # Colour: a smooth spatial gradient that only DRIFTS (loudness-scaled),
        # never jumps on a beat — colour never strobes, brightness carries the beat.
        self.colour_phase += (p.colour_speed + p.colour_flow * frame.energy) * dt * music_gate

        tr = self._mel_transient  # per-bin fresh-attack transients (the peaks)
        pan = getattr(frame, "pan", None)
        lf = self._light_flash
        for cid in lf:  # per-lamp peak flash fades each frame
            lf[cid] *= p.flash_decay

        colour_lerp = p.colour_lerp
        out: dict[int, RGB] = {}
        for ch in self.channels:
            cid = ch.channel_id
            info = self.cmap[cid]
            lo, hi = info["mel_lo"], info["mel_hi"]
            # GLOW: this lamp's band loudness (pan-weighted toward its side).
            level = _melbank_drive(frame, self._env, info, p.pan_gain)
            # PEAK: a fresh transient in this lamp's band, pan-weighted the same.
            peak = 0.0
            if tr and hi > lo:
                if p.pan_gain > 0.0 and pan and len(pan) >= hi:
                    side = 2.0 * info["nx"] - 1.0
                    peak = _pan_weighted_mean(tr, pan, lo, hi, side, p.pan_gain)
                else:
                    peak = sum(tr[lo:hi]) / (hi - lo)
            flash = peak * p.spectral_pop * music_gate  # proportional to peak height
            if flash > lf.get(cid, 0.0):
                lf[cid] = flash
            # Smoothed glow target = band loudness + a little whole-room energy.
            target = (
                p.melbank_floor
                + p.melbank_gain * level * music_gate
                + p.energy_gain * self._energy_env
            )
            prev_c, prev_b = self._state[cid]
            alpha = p.bri_attack if target >= prev_b else p.bri_decay
            new_b = prev_b + (target - prev_b) * alpha
            # Colour by spectral position (spatial gradient) + the drift phase.
            cpos = info["xrank"] * p.colour_spread + self.colour_phase
            tgt_c = self.palette.sample(cpos)
            m = max(tgt_c)
            tgt_c = (tgt_c[0] / m, tgt_c[1] / m, tgt_c[2] / m) if m > 1e-6 else (0.0, 0.0, 0.0)
            nc = (
                prev_c[0] + (tgt_c[0] - prev_c[0]) * colour_lerp,
                prev_c[1] + (tgt_c[1] - prev_c[1]) * colour_lerp,
                prev_c[2] + (tgt_c[2] - prev_c[2]) * colour_lerp,
            )
            self._state[cid] = (nc, new_b)
            if p.colour_sat < 1.0:
                s = p.colour_sat
                nc = (nc[0] * s + (1 - s), nc[1] * s + (1 - s), nc[2] * s + (1 - s))
            cval = max(nc)
            # Glow (smoothed) + peak flash (sharp), theme-faithful value, master.
            b = min(1.0, new_b + lf.get(cid, 0.0)) * (0.35 + 0.65 * cval) * self.brightness
            out[ch.channel_id] = (nc[0] * b, nc[1] * b, nc[2] * b)
        return out

    def _spawn_waves(
        self,
        p,
        frame: AnalysisFrame,
        beatgrid: BeatGrid | None,
        vis_strength: float,
        gate: float = 1.0,
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
        # Silence gate x salience: no wavefronts without audio (item 2), and a
        # quiet passage's waves ripple in proportion to its real loudness.
        strength *= gate
        if strength <= 0.0:
            return
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))
        # Waves launch from the current phrase's origin (centre/left/right
        # cycle) so long sections sweep the room from changing directions.
        oi = self._phrase % len(self._origins) if p.phrase_bars > 0 else 0
        self._waves.append(
            Wave(
                origin=self._origins[oi],
                strength=strength * (0.5 + 0.5 * bass),
                speed=p.wave_speed,
                width=p.wave_width,
                origin_idx=oi,
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
        # Silence gate (item 2 — only audio moves lights): the smoothed room
        # loudness fades out on a paused / finished / silent track, so every
        # beat reaction (flash, colour jump, wavefront) fades with it and stops
        # entirely in silence. A scheduled or stray beat can never strobe a
        # quiet room. The continuous melbank layer is gated the same way.
        music_gate = min(1.0, self._loud / _SILENCE_GATE) if _SILENCE_GATE > 0 else 1.0
        # Refresh the instrument-role assignments (rotate on schedule + drops).
        self._update_roles(p, frame, beatgrid, structure)
        # Advance the musical phrase (4-bar) counters on locked downbeats.
        self._update_phrase(p, beatgrid, music_gate)
        # The event-selection gates (precision upgrade): amplitude proportional
        # to the frame's ABSOLUTE track-relative loudness, and a width gate
        # muting narrowband (vocal/tonal) detections. Both come from the mode,
        # so Subtle picks strictly and Extreme stays permissive.
        amp_scale, width_gate = event_gates(p, frame.salience, frame.onset_width)
        # One decision for everything visible: the scheduled beat (locked) or
        # the qualifying kick (unlocked reactive fallback).
        vis_strength, vis_bass, grid_locked = self._visible_event(
            frame, beatgrid, width_gate
        )
        # Onset-flux gate (ModeParams.flux_gate): a beat only counts where a REAL
        # transient happened. A scheduled grid beat — especially from an offline
        # track map that force-fits a tempo grid across the WHOLE song — keeps
        # ticking through a tail / breakdown / vocal after the drums have stopped,
        # so the engine would flash, jump colour and roll WAVES on those phantom
        # beats ("predictive beats" the user does not want). ``flux_factor`` is
        # how real this beat is: a LOW-band transient (a kick — passes at any
        # onset width, so a tight electronic kick still counts) OR a MID-band
        # transient that is broadband (a guitar/snare — but a narrowband mid, i.e.
        # a sung vowel, is muted by the width gate). A held tone / vocal / tail
        # has neither, so it is muted at the source and nothing downstream —
        # flash, colour jump, highlight ranking, waves — fires. 0 disables.
        flux_factor = 1.0
        if p.flux_gate > 0.0:
            lo = 0.35 * p.flux_gate
            span = max(1e-6, p.flux_gate - lo)
            bass_g = max(0.0, min(1.0, (frame.bass_flux - lo) / span))
            mid_g = max(0.0, min(1.0, (frame.mid_flux - lo) / span)) * width_gate
            flux_factor = max(bass_g, mid_g)
            vis_strength *= flux_factor
        # Does the song currently have an actual beat? While the grid is
        # unlocked (no rhythm found), detected-onset flashes and waves soften
        # toward the mode's nobeat_flash floor, so beat-less passages breathe
        # with the continuous layers instead of strobing on false onsets. A
        # locked grid — or a couple of real kicks — restores full punch.
        self._update_rhythm_conf(frame, beatgrid)
        rhythm_gate = (
            p.nobeat_flash + (1.0 - p.nobeat_flash) * self._rhythm_conf
        )
        # Pre-drop anticipation: in the final moments before a drop the room
        # pulls in (dimmer, tighter, desaturated) so the detonation lands out
        # of held-back tension. ``pd`` scales every application below; the
        # music gate keeps a fade-to-silence from freezing a stuck dim.
        pd = p.predrop_depth * self._update_predrop(p, structure) * music_gate
        # Scale the flash by the beat's actual HEIGHT (item 3 / fade-outs): the
        # scheduled accent is normalised to the passage, so without this a track
        # fading out keeps flashing full on tiny beats. The smoothed loudness
        # follows the fade down, so flashes shrink with the music. The salience
        # amplitude composes by min() (not multiply — no double attenuation):
        # the legacy term is ~1 outside fade-outs, so the absolute
        # proportionality dominates — a quiet pluck pulses small, the drop
        # slams full (salience saturates at 1, it never amplifies).
        loud_scale = min(
            min(1.0, max(vis_bass, self._energy_env) / _LOUD_REF),
            amp_scale,
        )
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
                (frame.bass_beat and width_gate > 0.0)
                or (not grid_locked)
                or (beatgrid is not None and beatgrid.predicted_beat)
            )
            # Don't let silence-frame "beats" pollute the highlight ranking.
            highlight = self._beat_highlight(p, acc_now, append=beat_now and music_gate > 0.5)
        # Mid (guitar/snare) onsets from their own dedicated detector stream.
        # They only ever reach the mid-role lights, so they read as "that light
        # is the guitar". When the grid is locked they're quantised to the
        # eighth-note slots: syncopated riffs live on that grid, vocal
        # syllables float between its slots — the other half of the old
        # "pops on the singing" problem.
        # The width gate applies here too — the third, complementary vocal
        # guard next to the attack machine and the eighth-note quantiser.
        mid_strength = frame.mid_strength * width_gate if frame.mid_beat else 0.0
        if mid_strength > 0.0 and grid_locked and beatgrid is not None:
            ph = beatgrid.phase % 0.5
            if min(ph, 0.5 - ph) > _EIGHTH_PHASE:
                mid_strength = 0.0
        # Same onset-flux gate on the mid stream (guitar/snare): a scheduled mid
        # beat with no real mid-band transient (a held tone) is muted.
        if p.flux_gate > 0.0 and mid_strength > 0.0:
            lo = 0.35 * p.flux_gate
            mid_strength *= max(
                0.0, min(1.0, (frame.mid_flux - lo) / max(1e-6, p.flux_gate - lo))
            )
        # Advance the palette position. Colour-jump modes make colour the
        # PRIMARY motion (the apartment-sync look): the whole room snaps to a
        # new palette position on EVERY beat — a big spectrum-spanning jump —
        # with highlights jumping further still, so the colour reads as the
        # beat. Between beats it holds (only a slow drift). Legacy modes keep
        # the fluid continuous roll (the Hue+Spotify look).
        # All colour motion is gated by the silence gate (item 2): the slow
        # drift and the loudness-scaled flow both freeze when the audio stops,
        # so a paused/finished/silent track holds its colour instead of drifting.
        self.colour_phase += p.colour_speed * dt * music_gate
        # Continuous, loudness-scaled colour drift (LedFx-style): colour keeps
        # moving with the music between beats, so the show never freezes when no
        # beat is detected or the grid is unlocked. Beat colour-jumps add on top.
        if p.colour_flow > 0.0:
            self.colour_phase += (
                p.colour_flow * (0.25 + 0.75 * frame.energy) * dt * music_gate
            )
        sect = 0.6 + 0.4 * self.section_level
        rolling = (
            beatgrid is not None
            and beatgrid.locked
            and beatgrid.period_s > 0.05
            and p.colour_beat_step > 0.0
        )
        if self.manual_only:
            # Drum mode: colour moves only via the continuous drift above and
            # your taps; the automatic per-beat colour jumps are suppressed.
            pass
        elif p.colour_jump > 0.0:
            if beat_now:
                # Every beat jumps; highlights jump ~1.7x so the standout hits
                # land the biggest hue change. Scaled by the silence gate so the
                # colour stops moving when the audio stops.
                step = p.colour_jump * (0.55 + 0.45 * acc_now) * music_gate
                if highlight:
                    step *= 1.7
                # Phrase variation: the jump size breathes across phrases.
                if p.phrase_bars > 0:
                    step *= _PHRASE_JUMP[self._phrase % len(_PHRASE_JUMP)]
                # Hold the colour back through the pre-drop so the drop's
                # jump reads bigger against the stillness.
                self.colour_phase += step * sect * (1.0 - 0.5 * pd)
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
            # drum mode: no automatic wavefronts; silence: none either.
            if not self.manual_only and music_gate > 0.0:
                self._spawn_waves(
                    p,
                    frame,
                    beatgrid,
                    vis_strength,
                    music_gate
                    * amp_scale
                    * (1.0 if grid_locked else rhythm_gate)
                    * (1.0 - 0.8 * pd)
                    # Only real onsets roll a wave: without this the track map's
                    # phantom grid beats keep spawning waves that sweep the room
                    # for seconds AFTER the last drum (the "reacting afterwards").
                    * flux_factor,
                )
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
            kick = kick_flash(p, vis_strength, vis_bass) * flash_scale * rhythm_gate
        # Silence gate (item 2: vanish in a gap) × loudness scale (item 3: a
        # fading beat brightens only as much as its height).
        kick *= music_gate * loud_scale
        midf = mid_flash(p, mid_strength) * music_gate * loud_scale
        if not grid_locked:
            midf *= rhythm_gate
        # Pre-drop tightening: hold the pulse back through the pull-down —
        # except the bar's downbeat, so the room never loses the count.
        if pd > 0.0:
            tighten = 1.0 - 0.8 * pd
            if not (grid_locked and beatgrid is not None and beatgrid.beat_in_bar == 0):
                kick *= tighten
            midf *= tighten
        # The full-room moment: the passage's very biggest hits take EVERY
        # light at once (vocal lights a touch softer), the way the reference
        # shows punctuate a chorus. Ordinary highlights stay role-separated.
        full_room = kick > 0.0 and highlight and acc_now >= p.full_room_accent
        lf = self._light_flash
        decay = p.flash_decay  # per-mode: lower = snappier firework fall
        for cid in lf:
            lf[cid] *= decay
        if not self.manual_only and (kick > 0.0 or midf > 0.0):
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
            # Builds and the pre-drop both desaturate; compose by max (never
            # sum) so overlapping tension can't double-wash the colour.
            sat_mul = 1.0 - max(
                p.build_desat * structure.build_progress, 0.6 * pd
            )
            drop = 0.0
            if structure.drop_now:
                # A drop that detonates out of a held pre-drop swells bigger:
                # the released depth is the tension it earned.
                drop = p.drop_boost * (1.0 + 0.35 * self.predrop_released)
            self._swell = max(self._swell * 0.85, drop)
            alpha = 1.0 - math.exp(-dt / 2.0)
            self.section_level += (structure.section_level - self.section_level) * alpha
        else:
            self._swell *= 0.85

        targets = render(self, frame)

        colour_lerp = p.colour_lerp
        attack, decay = p.bri_attack, p.bri_decay
        slew = p.bri_slew  # max emitted brightness RISE per frame (anti-strobe)
        out: dict[int, RGB] = {}
        for cid, (target_color, target_b) in targets.items():
            # Pre-drop pull-down: compress the brightness HEADROOM above the
            # mode's floor (base == floor modes are provably untouched, and
            # the silence gate keeps priority since pd carries music_gate).
            if pd > 0.0:
                target_b = p.floor + (target_b - p.floor) * (1.0 - pd)
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
            # Continuous brightness + flash/swell, slew-limited so a beat reads
            # as a fast dim<->bright SWING rather than a 1-frame strobe. The rise
            # is capped at ``bri_slew`` per frame (≈full in 1/bri_slew frames);
            # falls pass through freely so the room still dims quickly between
            # hits. bri_slew == 1.0 keeps the old instant snap (calm modes).
            b = min(1.0, new_b + overlay)
            prev_emit = self._emit_b.get(cid, 0.0)
            if slew < 1.0 and b > prev_emit + slew:
                b = prev_emit + slew
            self._emit_b[cid] = b
            b *= self.brightness
            out[cid] = (nc[0] * b, nc[1] * b, nc[2] * b)
        return out
