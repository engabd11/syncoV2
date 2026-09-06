"""Intensity modes as parameters for one unified renderer, with **band roles**.

Two layers of instrument reactivity work together. **Named roles** give a few
lights a dedicated job — **bass** (kick), **mid** (guitar/snare) or **vocal**
(shimmer on singing) per ``role_mix`` — and rotate musically so the show keeps
surprising. With ``dynamic_roles`` the split is also weighted by which bands are
actually playing (per-band *presence*), re-dealt on each rotation: a track with
no guitar hands its mid lamps to the bass/vocals that ARE there, so no lamp sits
dull on an absent instrument, and the band re-forms as the song's instruments
come and go. On top of that, **every** light reacts to its own slice of the full
melbank spectrum (``spectral_pop``): it pops on a fresh attack in its frequency
range, so a kick lights the low lamps, a snare the low-mids, a guitar/lead the
mids and a cymbal the highs. The room therefore adapts to *all* instruments and
all kinds of music, not just the three named roles.

The signature look (measured frame-by-frame from the apartment-sync reference
recording) has three parts the high modes deliver together:

* **Colour is the show.** The whole room holds ONE unified hue and JUMPS to a
  new one — a big, spectrum-spanning step — on every beat. Colour, not
  brightness, is the primary motion; it reads as the beat.
* **Highlight selection.** Brightness slams bright only on the beats that stand
  out in their passage (ranked against the recent ~24 beats) and falls back to
  dark between them — the reference sits fully dark ~37% of the time. A flat
  four-to-the-floor still hits every beat; a dynamic mix fires only the
  standouts.
* **A real dark room.** Base brightness is ~0; the chorus is lit by the song's
  own energy, the breakdown goes black, and the flashes punch out of it.

Every rung also applies the **event-salience precision gates** (see
:func:`event_gates`): flash amplitude follows the frame's absolute
track-relative loudness (a quiet pluck pulses small, the drop slams full) and
narrowband onsets (sung vowels, sustained tones) are muted — Subtle picks the
strictest, Extreme the loosest.

The ladder — same pattern throughout, each rung harder, darker and more unified:

* **Subtle** — no dimming; one gentle spatial gradient, colour drifts + small
  per-beat steps. The calm preset.
* **Medium** — gentle club: visible dimming, soft flashes on the stronger
  beats, album colours stepping each beat across a wide spatial spread.
* **High** — the one mode that keeps the per-instrument SPATIAL split: bass
  lights snap on kicks, guitar lights pop on mid onsets, vocal lights shimmer
  with the singing; roles rotate every few bars.
* **Intense** — *unrestrained*: a fast dim<->bright SWING — the room brightens on
  the beat over a few frames (a slew-limited swell, not a 1-frame strobe) and the
  colour shifts each hit — over a soft glow that never quite goes black.
  Eye-safety limiter bypassed (see safety docs).
* **Extreme** — a ground-up rebuild that treats *the song as a graph* and reacts
  to its shape directly, ignoring the beat grid entirely (``graph_reactive``, see
  :meth:`EffectEngine._render_extreme`). Every lamp owns a slice of the spectrum
  by its left-right position — low frequencies on one side, highs on the other —
  so instruments **separate in space**: a kick lights the low lamps, a snare the
  low-mids, a lead the mids, a cymbal the highs, all at once. Each lamp carries
  two things: a **glow** proportional to how loud its band is right now, and a
  **flash** proportional to a fresh transient (a *peak*) in that band — higher
  peak, brighter flash; lower peak, dimmer flash. A sustained tone (a held vocal,
  a pad) therefore only *glows* in proportion to its loudness and never strobes,
  because a steady graph has no fresh peaks; only genuine attacks flash. There is
  no beat grid, no highlight selection and no phantom/predicted beats — the room
  is a live readout of the actual spectrum, so intros, grooves, vocals and
  fade-outs all read honestly. Colour drifts smoothly across the spectrum rather
  than jumping. The eye-safety limiter is bypassed entirely (see safety docs).
  Intense and every lower rung are unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from ..const import (
    AUTO_BPM_HIGH,
    AUTO_BPM_LOW,
    AUTO_BPM_MARGIN,
    DEFAULT_AUTO_LEVELS,
    INTENSITY_LADDER,
    SyncMode,
)
from ..color.palette import RGB


@dataclass(frozen=True, slots=True)
class ModeParams:
    base: float          # steady brightness between beats
    floor: float         # minimum brightness (darkness between beats)
    bass_gain: float     # continuous brightness from a light's role envelope
    beat_gain: float     # pop of a bass-role light on a (qualifying) kick
    beat_threshold: float  # only kicks this strong pop (higher = big beats only)
    spread: float        # per-light spectrum variety (legacy, role-less modes)
    colour_speed: float  # palette drift per second (continuous, time-based)
    shimmer: float       # sparkle amount (vocal-role lights; all when role-less)
    colour_sat: float = 1.0  # <1 softens colours toward white (Samsung-style)
    colour_beat_step: float = 0.0  # palette advance per beat (colour moves to the beat)
    colour_lerp: float = 0.16  # per-frame colour easing (higher = snappier shifts)
    energy_gain: float = 0.0  # brightness from broadband loudness (ambient/movie)
    bri_attack: float = 0.92  # per-frame brightness rise rate (1 = instant)
    bri_decay: float = 0.24  # per-frame brightness fall rate (lower = gentler)
    flash_decay: float = 0.80  # per-frame fade of the beat-flash burst (lower =
    #                            snappier, more strobe-like firework fall)
    bri_rise_rate: float = 16.0  # ceiling on emitted-brightness RISE, full scale
    #                            per second. Replaced the per-frame ``bri_slew``
    #                            cap (rise-only, disabled on three of five rungs),
    #                            under which a beat attack was a single-frame
    #                            discontinuity — what bulbs render as a hard edge
    #                            and what the rate limiter quantised into a
    #                            staircase. Philips' guidance is that people are
    #                            far more sensitive to rapid brightness changes
    #                            than to rapid colour changes, so the brightness
    #                            transition should be the *slower* of the two;
    #                            these stay under the encoder's xy slew
    #                            (~4.8 full scale/s at the 12-bit xy slew cap)
    #                            and the fall rate below stays well under the
    #                            rise.
    bri_fall_rate: float = 4.0  # ceiling on emitted-brightness FALL, full scale
    #                            per second. See ``bri_rise_rate``. The fall is
    #                            deliberately the looser of the two only in
    #                            ratio to the music: it buys the smooth dimming
    #                            between beats that an unlimited fall (the old
    #                            behaviour) flattened into a hard cut.
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
    # --- per-bulb instrument roles -------------------------------------------
    # Fractions of the lights acting as (bass, mid/guitar, vocal/shimmer).
    # (1, 0, 0) = every light rides the bass (the classic single-stream look).
    role_mix: tuple[float, float, float] = (1.0, 0.0, 0.0)
    mid_gain: float = 0.0      # pop of a mid-role light on a guitar/snare onset
    mid_threshold: float = 1.3  # onset strength a mid hit needs to pop
    vocal_dim: float = 0.08    # very dim base of vocal-role lights
    role_rotate_beats: int = 0  # swap role assignments every N beats (0 = never)
    dynamic_roles: bool = False  # weight the role split by which bands are
    #                             actually playing (no lamp stuck on a dead
    #                             instrument), re-dealt on each rotation
    hard_snap: bool = False    # snap on top of the wave instead of yielding to it
    # --- scheduled (grid-locked) pulse shaping --------------------------------
    # When the tempo grid is locked, EVERY beat fires a pulse (the Samsung /
    # Hue+Spotify metronome feel); these shape how big each one is.
    accent_floor: float = 0.0   # accents below this barely register (selectivity)
    weak_pulse: float = 0.30    # fraction of beat_gain a zero-accent beat still gets
    downbeat_pulse: float = 0.0  # minimum pulse weight on bar downbeats
    # --- highlight selection (the apartment-sync look) ------------------------
    # A beat is a *highlight* when its accent ranks in the top (1-q) of the
    # recent beats (rolling window in the engine): selectivity that adapts to
    # the passage, where a fixed accent threshold goes blind (a flat passage
    # all-fires or all-skips). 0 disables ranking — every scheduled beat is a
    # highlight, the pre-highlight behaviour.
    highlight_quantile: float = 0.0
    # Colour as the PRIMARY motion (the apartment-sync look): the whole room
    # jumps to a new palette position on every beat (highlights jump further),
    # so colour reads as the beat. 0 keeps the legacy continuous-roll colour.
    colour_jump: float = 0.0
    # Per-lamp hue variation: 1.0 = the lamps span the gradient (a spatial
    # rainbow); 0.0 = every lamp shows the SAME hue (a unified room that jumps
    # colour together — what the reference does at high intensity).
    colour_spread: float = 1.0
    full_room_accent: float = 2.0  # accent at/above which ALL roles slam (2 = never)
    # --- LedFx-style continuous reactive layer --------------------------------
    # The "always alive" foundation: each lamp rides the exp-smoothed power of
    # its slice of the melbank (mapped across the room, LedFx "Wavelength"),
    # independent of any beat detection. Beat flashes/waves/colour-jumps ride ON
    # TOP of this, so a missed or mistimed beat only removes punch — it can never
    # make the room go dark while music is playing.
    melbank_gain: float = 0.0   # continuous brightness from the lamp's melbank slice
    melbank_floor: float = 0.0  # small ambient lift while music plays (keeps slow
    #                             ramps off the bridge's coarse low-value range)
    colour_flow: float = 0.0    # continuous palette advance per second (loudness-scaled),
    #                             so colour keeps moving between beats too
    spectral_pop: float = 0.0   # transient pop per lamp from a fresh attack in its
    #                             melbank slice: reacts to EVERY instrument across the
    #                             spectrum (kick/snare/guitar/lead/cymbal), not just roles
    # --- event-salience precision gates (proportional reactions) --------------
    # Flash amplitude follows the frame's ABSOLUTE loudness relative to the
    # track (frame.salience): a quiet pluck gives a small dim pulse, the drop
    # slams full. gamma shapes the ladder's strictness: >1 compresses quiet
    # events harder (Subtle), <1 lets them through (Extreme).
    salience_gamma: float = 1.0
    salience_floor: float = 0.05  # a real detected beat never renders at literal 0
    # Detected onsets narrower than width_min across the SuperFlux filterbank
    # (frame.onset_width — vocals/sustained tones are narrowband, drums are
    # broadband) are fully muted; a smoothstep knee of width_soft above it
    # keeps borderline onsets from flickering in and out.
    width_min: float = 0.0
    width_soft: float = 0.10
    # The bass-content weight floor in kick_flash (was a hard-coded 0.4):
    # how much a bass-less onset may still flash.
    kick_bass_floor: float = 0.40
    # When set, the engine renders this mode with the dedicated "song as a graph"
    # path (EffectEngine._render_extreme) instead of the beat-grid renderer: each
    # lamp reflects its own slice of the spectrum (glow = band loudness, flash =
    # band transient, both proportional), instruments separate by frequency across
    # the room, and the beat grid is ignored entirely (no phantom/predicted
    # beats). Only Extreme uses it; here melbank_gain is the glow gain and
    # spectral_pop the peak-flash gain.
    graph_reactive: bool = False
    # --- Extreme graph-renderer enrichment (only _render_extreme reads these) ---
    # Weight of the per-bin spectral FLUX (frame-to-frame rise) in a lamp's peak
    # flash, on top of the slow-baseline transient. Flux re-fires on EVERY hit in
    # a steady groove — each attack rises again after the inter-hit decay — so a
    # driving hi-hat / bassline / riff keeps the lamps moving instead of being
    # absorbed into the baseline and going quiet (the fix for "only the big beats
    # register, the rest of the song is missing"). A sustained tone does not rise
    # frame-to-frame, so it still only glows and never strobes. 0 = pure novelty
    # (the original v1.40 behaviour).
    mel_flux_gain: float = 0.0
    # Noise floor on that per-bin flux (0 disables): a rise smaller than this is
    # treated as room tone / ambience / reverb wash and contributes NOTHING, so
    # the flashes fire on real instrument/beat attacks instead of "every sound".
    # The novelty transient is deliberately left un-gated (it is already
    # selective — a sustained sound is absorbed into its baseline), so this only
    # tames the groove flux back to 1.40-style selectivity.
    mel_flux_floor: float = 0.0
    # Spectral ROTATION speed, in lamp-steps per second (0 = fixed mapping). The
    # lamp<->spectrum assignment slowly rotates around the room so every lamp
    # takes turns being the kick / snare / guitar / cymbal — the whole room trades
    # instruments as the song goes on instead of each lamp being pinned to one
    # band forever. Grid-free (advances on time, scaled by loudness), so it adds
    # no phantom/predicted beats. Full spectral coverage is preserved at every
    # instant; only *which lamp* shows *which* band cycles.
    rotate_rate: float = 0.0
    # Extra rotation speed scaled by the frame's energy (0 = constant rate). The
    # room rotates FASTER through busy/loud passages and settles in the quiet
    # ones, so the instrument shuffle feels musical instead of a metronomic drift.
    rotate_swing: float = 0.0
    # Contrast expansion on a lamp's peak flash before it lights (1.0 = linear).
    # >1 pushes small peaks much dimmer while leaving big ones bright, so peaks
    # get RELATIVE brightness — a light tick barely lifts, a real hit slams — the
    # room stops reading uniformly bright because "every peak is a full flash".
    flash_gamma: float = 1.0
    # Absolute-loudness floor on the flash (1.0 = ignore loudness). The peak-flash
    # is scaled by ``floor + (1-floor)*salience`` so a hit in a whisper-quiet
    # passage can't flash as bright as the same hit in a drop — the AGC'd melbank
    # is *relative*, so without this a quiet tick and a loud slam look identical.
    flash_loud_floor: float = 1.0
    # How strongly a lamp's brightness follows its band's ABSOLUTE loudness
    # (0 = off / every band equal, the per-bin-normalised default). Uses the
    # offline melbank_ref: a loud band (kick) lights its lamp brighter than a
    # quiet one (a faint cymbal tick), which per-bin normalisation flattens.
    # Perceptually compressed so a quiet instrument stays visible, just dimmer.
    band_loud_strength: float = 0.0
    # Whole-room SLAM on a big BROADBAND transient, Extreme only (0 = off). A
    # kick/drop that spikes many melbank bands at once punches the ENTIRE room in
    # unison on top of the per-band flashes — the impact that makes the big
    # moments land — while a single-band tick (an isolated hi-hat) barely
    # registers, so the per-band detail is untouched. Squared, so only genuinely
    # big broadband hits slam; moderate hits lift the room only slightly.
    room_punch: float = 0.0
    # ONSET-FLUX gate (0 disables). A *scheduled* beat (from the offline track
    # map's tempo grid, or the causal tracker) fires on the grid even where no
    # real onset happened — an offline map force-fits a grid across the WHOLE
    # song, so it keeps ticking beats through a tail/breakdown after the drums
    # have stopped, and the engine flashes each one (the "strobing after the
    # last beat" bug). Gating the flash by the frame's actual onset flux
    # (bass_flux for the kick, mid_flux for the mid) suppresses those phantom
    # beats — a real drum has a flux spike, a held tone / vocal does not — so a
    # flash only lands on a genuine transient PEAK. This value is the flux level
    # that earns a full flash; below ~0.3x of it the flash is fully muted.
    flux_gate: float = 0.0
    # Flash floor while the song has NO discernible beat (0..1): detected-onset
    # flashes and waves scale between this and full with the engine's
    # rhythm-confidence envelope (tempo lock, or broadband kicks while
    # unlocked). The permissive club modes need it: their width/salience gates
    # deliberately let nearly every onset through, so a beat-less passage
    # (pads, vocals, ambience) otherwise strobes a dark room on false onsets.
    # 1.0 = no gating (the strict modes' gates already handle this).
    nobeat_flash: float = 1.0
    # Pre-drop pull-down depth (0..1): how far the room tightens in the final
    # moments before a drop (the classic pro-lighting anticipation — dim,
    # desaturate, hold back the pulse, then detonate). Scales the brightness
    # HEADROOM above the mode's floor, so a mode with base == floor is
    # provably inert. 0 disables. Scheduled drops (track-map boundaries) ramp
    # against their known ETA; the heuristic path commits conservatively (see
    # engine._update_predrop).
    predrop_depth: float = 0.0
    # --- phrase-level evolution ------------------------------------------------
    # Long steady sections shouldn't feel like a loop: every ``phrase_bars``
    # locked bars the engine advances a phrase counter that cycles the wave
    # origin around the room (centre/left/right), varies the colour-jump
    # magnitude, and nudges the palette forward by ``phrase_colour_shift`` —
    # all deterministic, so the variation is musical rather than random.
    # 0 disables (Subtle stays perfectly steady).
    phrase_bars: int = 0
    phrase_colour_shift: float = 0.0
    # --- stereo pan spatial mapping --------------------------------------------
    # How strongly a lamp's melbank slice is weighted toward its side of the
    # stereo field (frame.pan): a synth panned hard right brightens the
    # right-hand lamps and fades from the left ones. 0 disables; frames
    # without pan (mono taps, pre-v4 maps) always render exactly as before.
    pan_gain: float = 0.0

    # --- Sustain bloom (P3, from CAMusic): a slow room-wide glow that rises
    # on sustained, pitched, mid-heavy material — held vocals/pads bloom
    # instead of reading dark. Zero gain disables the layer entirely.
    tonal_gain: float = 0.0       # brightness a fully-committed sustain adds
    tonal_width_max: float = 0.22 # onset_width at/above which nothing is tonal
    tonal_width_soft: float = 0.10  # soft knee below tonal_width_max
    tonal_attack_s: float = 0.55  # rise time constant (s): blooms, not pulses
    tonal_release_s: float = 1.30  # fall time constant (s): lingers
    tonal_damp: float = 1.0       # how hard live transients suppress the bloom

    # --- melbank shape (P3, from CAMusic): the melbank drive blends the
    # slice's mean with its hottest bin (a held vocal occupies 2-3 of a
    # lamp's ~7 bins, so a pure mean delivered it at a third of its real
    # height), and per-bin absolute-loudness weights (mean-normalised) put
    # how LOUD each band is back into the per-bin AGC'd melbank on every
    # rung ( Extreme keeps its raw attenuating form — see
    # EffectEngine.melbank_loud_weights).
    mel_peakiness: float = 0.0    # 0 = pure mean, 1 = pure hottest bin


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # Seamless: NO dimming whatsoever (base == floor) — the lights hold a steady
    # bright level and only the colour moves: a slow drift plus a small step on
    # each beat, spread across the lamps as a gentle spatial gradient.
    SyncMode.SUBTLE: ModeParams(
        base=0.80, floor=0.80, bass_gain=0.0, beat_gain=0.0, beat_threshold=99.0,
        spread=0.0, colour_speed=0.04, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.008, colour_lerp=0.10, bri_attack=0.12, bri_decay=0.08,
        highlight_quantile=0.0, colour_jump=0.020, colour_spread=1.0,
        # Width calibration (synthetic measurement, see tests/test_salience.py):
        # kick with attack click ~0.9, isolated sine kick ~0.49, kicks buried
        # in a dense noise bed 0.13-0.31, sung-vowel onsets 0.11-0.13. The
        # ladder brackets that vocal band: Subtle mutes anything not clearly
        # broadband, High cuts right above the vowel cluster, Extreme keeps
        # all but the purest tones.
        salience_gamma=1.6, width_min=0.20,
        bri_rise_rate=4.0, bri_fall_rate=1.5,
        mel_peakiness=0.20, band_loud_strength=0.15,
    ),
    # Gentle club: visible dimming, soft flashes on the stronger beats, album
    # colours stepping each beat across a wide spatial spread. The calmest of
    # the colour-jump modes.
    SyncMode.MEDIUM: ModeParams(
        base=0.12, floor=0.05, bass_gain=0.14, beat_gain=0.9, beat_threshold=1.4,
        spread=0.0, colour_speed=0.05, shimmer=0.10, colour_sat=0.7,
        colour_beat_step=0.0, colour_lerp=0.40, bri_attack=1.0, bri_decay=0.30,
        bri_rise_rate=16.0, bri_fall_rate=3.0,
        wave_gain=0.75, wave_speed=2.2, wave_width=0.30, height_freq=0.30,
        depth_wash=0.08, anticipation_ms=80, drop_boost=0.50, build_desat=0.50,
        role_mix=(1.0, 0.0, 0.0),
        highlight_quantile=0.30, weak_pulse=0.25, downbeat_pulse=0.40,
        colour_jump=0.045, colour_spread=0.70,
        energy_gain=0.15,
        melbank_gain=0.45, melbank_floor=0.06, colour_flow=0.05, spectral_pop=0.35,
        salience_gamma=1.3, width_min=0.15, kick_bass_floor=0.30,
        predrop_depth=0.30, phrase_bars=4, phrase_colour_shift=0.03,
        pan_gain=0.5,
        mel_peakiness=0.35, band_loud_strength=0.35,
        tonal_gain=0.22, tonal_attack_s=0.65, tonal_release_s=1.5,
    ),
    # The band on your lights: bass lights snap on kicks, guitar lights pop on
    # mid onsets, and vocal lights shimmer dimly with the singing — assignments
    # rotate every 4 bars. The one mode that keeps the per-instrument SPATIAL
    # split (others go unified). Tuned to breathe: a stronger continuous melbank /
    # energy layer, gentler fade and longer flash glow so atmospheric, groove-led
    # albums (Lumin Rain and the like) flow while the beat still clearly lands —
    # the everyday mode that fits most music.
    SyncMode.HIGH: ModeParams(
        base=0.06, floor=0.035, bass_gain=0.30, beat_gain=1.6, beat_threshold=1.1,
        spread=0.0, colour_speed=0.06, shimmer=0.50, colour_sat=0.8,
        colour_beat_step=0.0, colour_lerp=0.38, bri_attack=1.0, bri_decay=0.38,
        bri_rise_rate=20.0, bri_fall_rate=4.0,
        wave_gain=0.55, wave_speed=2.2, wave_width=0.32,
        anticipation_ms=80, drop_boost=0.60, build_desat=0.45,
        role_mix=(0.4, 0.3, 0.3), mid_gain=1.0, mid_threshold=1.25,
        vocal_dim=0.05, role_rotate_beats=16, dynamic_roles=True, hard_snap=True,
        flash_decay=0.80,
        highlight_quantile=0.40, weak_pulse=0.16, downbeat_pulse=0.45,
        colour_jump=0.09, colour_spread=0.55, full_room_accent=0.94,
        energy_gain=0.15,
        melbank_gain=0.44, melbank_floor=0.035, colour_flow=0.05, spectral_pop=0.45,
        salience_gamma=1.0, width_min=0.12, kick_bass_floor=0.35,
        mel_peakiness=0.40, band_loud_strength=0.45,
        tonal_gain=0.26,
        predrop_depth=0.45, phrase_bars=4, phrase_colour_shift=0.05,
        pan_gain=0.6,
    ),
    # UNRESTRAINED (eye-safety limiter bypassed - explicit user choice, see
    # effects/safety.py). The SAME smooth dim<->bright SWING as Extreme - the
    # whole room breathes with the energy and brightens on the beat over a few
    # frames (flash_attack), colour shifting each beat - but with a HIGHER dark
    # FLOOR so it never drops to full black: a touch gentler and more
    # comfortable than Extreme while moving the same way. The floor is the
    # deliberate, only-real difference between the two (per the user): same
    # quick swing, Intense just keeps a soft glow in the gaps.
    SyncMode.INTENSE: ModeParams(
        base=0.05, floor=0.10, bass_gain=0.16, beat_gain=1.7, beat_threshold=1.0,
        spread=0.0, colour_speed=0.05, shimmer=0.0, colour_sat=0.97,
        colour_beat_step=0.0, colour_lerp=0.55, energy_gain=0.16,
        bri_attack=1.0, bri_decay=0.40,
        bri_rise_rate=24.0, bri_fall_rate=5.0, flash_decay=0.82,
        wave_gain=0.55, wave_speed=2.4, wave_width=0.30,
        anticipation_ms=90, drop_boost=0.80, build_desat=0.50,
        role_mix=(1.0, 0.0, 0.0), hard_snap=True,
        highlight_quantile=0.18, weak_pulse=0.42, downbeat_pulse=0.55,
        colour_jump=0.16, colour_spread=0.22, full_room_accent=0.0,
        melbank_gain=0.42, melbank_floor=0.06, colour_flow=0.05, spectral_pop=0.45,
        salience_gamma=0.8, width_min=0.08, nobeat_flash=0.30,
        mel_peakiness=0.40, band_loud_strength=0.40,
        tonal_gain=0.20, tonal_damp=1.4,
        # Phantom-beat guard: the offline track map force-fits a tempo grid across
        # the WHOLE song, so its scheduled beats keep ticking through tails and
        # breakdowns where the drums have stopped. Gate each scheduled beat by the
        # frame's real onset flux — a real hit has a flux spike, a phantom does
        # not — so the phantom flashes/colour-jumps/waves are muted while every
        # genuine beat passes untouched: real beats (flux ~0.7-1.0) pass at full
        # strength, while the phantom grid beats a track map fires into a tail /
        # outro (lower flux) are muted — the fix for the residual end-of-song
        # strobing. Frame-based, so the locked scheduled path stays confidence-
        # independent (a real beat carries flux regardless of history).
        flux_gate=0.5,
        predrop_depth=0.60, phrase_bars=4, phrase_colour_shift=0.06,
        pan_gain=0.5,
    ),
    # REBUILT FROM SCRATCH — "the song is a graph." Extreme uses its own direct
    # renderer (graph_reactive → EffectEngine._render_extreme) that ignores the
    # beat grid entirely and reacts to the actual spectrum, so there are no
    # phantom / predicted beats. Each lamp reflects its own slice of the audio:
    #   * a smooth GLOW proportional to that band's loudness (melbank_gain), so
    #     the room tracks the music going louder/quieter and a held tone / vocal
    #     just glows instead of strobing, and a song tail simply fades out, and
    #   * a PEAK FLASH proportional to a fresh transient in that band
    #     (spectral_pop) — a big peak flashes bright, a small one dim.
    # The lamps are spread left→right across the spectrum (+ stereo pan), so
    # instruments SEPARATE in 3D space: kick on the low lamps, snare/guitar on the
    # mids, cymbals on the highs, panned parts on their side. Colour is a smooth
    # spatial gradient that only drifts (never jumps), so colour never strobes.
    # The eye-safety limiter is bypassed (coordinator._bypass_limiter, README
    # warning). Only the fields the graph renderer reads are set below.
    SyncMode.EXTREME: ModeParams(
        graph_reactive=True,
        # Beat-path fields the graph renderer never reads (kept 0 / inert).
        bass_gain=0.0, beat_gain=0.0, beat_threshold=99.0, spread=0.0, shimmer=0.0,
        base=0.0, floor=0.0,
        melbank_gain=0.60, melbank_floor=0.02,   # GLOW: brightness ∝ band loudness (dark room)
        spectral_pop=1.6,                         # PEAK FLASH gain (lowered so peaks don't all saturate)
        flash_gamma=1.5,                          # expand peak contrast: small ticks stay dim, big hits slam
        flash_loud_floor=0.30,                    # and scale by absolute loudness (quiet hit ≠ drop hit)
        mel_flux_gain=1.25,                       # groove: every real hit re-fires, not just novel peaks
        mel_flux_floor=0.12,                      # but ignore ambient/noise wash — only real attacks
        rotate_rate=0.36,                         # base spectrum rotation ~one lamp every 3 s
        rotate_swing=0.85,                        # + much faster through busy passages (instruments circle the room)
        band_loud_strength=0.8,                   # loud bands (kick) brighter than quiet ones (cymbal tick)
        room_punch=1.5,                           # big broadband hits slam the WHOLE room ("Intense on steroids")
        energy_gain=0.06,                         # a touch of whole-room loudness lift (kept low)
        flash_decay=0.70,                         # per-frame fade of a peak flash
        bri_attack=0.5, bri_decay=0.4,            # glow smoothing (flash stays sharp)
        bri_rise_rate=26.0, bri_fall_rate=6.0,
        colour_speed=0.05, colour_flow=0.05,      # smooth colour drift (no beat jumps)
        colour_spread=0.4, colour_lerp=0.4, colour_sat=0.97,
        pan_gain=0.6,                             # stereo → light the matching side
    ),
}

# The club modes that opt out of the strict WCAG flash limiter — an explicit,
# documented user choice (see the README photosensitivity warning). INTENSE runs
# the RELAXED limiter (a high budget real music never hits, see
# safety.RELAXED_MAX_FLASHES_PER_S, still hard-capping pathological strobe).
# EXTREME goes further and bypasses the limiter ENTIRELY (coordinator.
# _bypass_limiter) for the sharpest, fastest flashing — the one fully-unlimited
# path. Subtle/Medium/High and the Movies effect always get the strict limiter.
UNRESTRAINED_MODES = frozenset({SyncMode.INTENSE, SyncMode.EXTREME})


def auto_mode_for_bpm(bpm: float, current: SyncMode) -> SyncMode:
    """Resolve the Auto intensity to a concrete Subtle/Medium/High from ``bpm``.

    Slow songs (< ``AUTO_BPM_LOW``) map to Subtle, up-tempo (> ``AUTO_BPM_HIGH``)
    to High, everything between to Medium. ``current`` is the level in effect
    now; a band change only commits once ``bpm`` crosses the *far* edge of the
    ±``AUTO_BPM_MARGIN`` dead-zone, so a track hovering on a boundary can't
    oscillate. Never returns Intense/Extreme — those stay manual-only.
    """
    lo, hi, m = AUTO_BPM_LOW, AUTO_BPM_HIGH, AUTO_BPM_MARGIN
    # The current band is sticky: its edge is pushed out by the margin, so you
    # must cross the far side of the dead-zone to leave it. While already Subtle
    # you stay Subtle up to lo + m; from a higher band you only drop to Subtle
    # once bpm falls below lo - m. Likewise High holds down to hi - m, but you
    # only climb into High above hi + m.
    low_edge = lo + m if current is SyncMode.SUBTLE else lo - m
    high_edge = hi - m if current is SyncMode.HIGH else hi + m
    if bpm < low_edge:
        return SyncMode.SUBTLE
    if bpm > high_edge:
        return SyncMode.HIGH
    return SyncMode.MEDIUM


# --- musical Auto intensity picker ------------------------------------------
# Rungs follow the music's REAL character, not each track's self-relative
# loudness. Three layers:
#
#   A. character — one absolute 0..1 score for the song (how percussive its
#      onsets are, how constantly they fire, tempo, low-end weight). Built only
#      from features that survive the analysis' per-track p95 normalisation, so
#      it compares ACROSS songs: a lofi track scores low however loud its own
#      chorus gets. Loudness deliberately isn't a term — it can't be one, since
#      every track is normalised to its own peak before the picker sees it.
#   B. the earned band — character maps to an absolute floor..ceiling on the
#      ladder. A chill song's ceiling is Medium; only a genuinely heavy track
#      earns a ceiling in Extreme. This is also where the floor comes from: a
#      house track simply can't reach Subtle, with no special-casing.
#   C. the moment — the song's own section curve moves within that band, so
#      verse↔chorus↔drop still switches on time.
#
# The enabled set (``allowed``) is a PALETTE, not a forced range: the absolute
# ladder position is remapped proportionally onto the rungs you picked, keeping
# each rung's relative prominence. A banger reaches the top of your selection, a
# chill track never does — and no song has to use every rung you enabled.
_SIG_LO_REF = 0.25   # ~a quiet intro sits at the bottom of the song's band
_SIG_HI_REF = 0.88   # ~a full drop sits at the top of it
# Anti-flicker dead-band on a rung boundary, a floor on seconds between committed
# switches (kept long so a switch is never rushed and the new rung has time to
# breathe), and the asymmetric smoothing on the raw signal: rise reasonably quick
# so a drop is caught, fall slow so a brief dip doesn't drop the room out of the
# chorus. The dead-band is a *fraction of a cell* rather than an absolute width,
# because rung cells are deliberately unequal (see ``_RUNG_SHARE``): a fixed
# 0.07 would swallow narrow Extreme whole. 0.35 of the narrower neighbouring cell
# reproduces the old feel exactly on the equal-width case it was tuned for.
_PICK_HYST_FRAC = 0.35
_PICK_DWELL_S = 3.5
# A *big* move (a real drop/breakdown — the target is this many ladder rungs from
# the current one) commits on a much shorter dwell, so the room reaches the new
# energy right as the section changes instead of stepping up one unhurried rung at
# a time. Small, one-rung adjustments keep the long dwell so the pick stays calm.
_PICK_BIG_JUMP = 2
_PICK_BIG_DWELL_S = 1.0
_PICK_ATTACK = 0.10   # per-frame EMA weight while the signal is rising
_PICK_DECAY = 0.03    # per-frame EMA weight while it is falling
# Section-level smoothing used ONLY on the per-song-profile path: rung selection
# should follow the song's *sections* (verse↔chorus↔drop), not individual beats.
# Mapping a much slower envelope keeps switches unhurried while still tracking
# real section changes. Asymmetric like the fast one (rise in ~1.2 s to catch a
# drop, fall over ~3.5 s so a brief dip doesn't leave the chorus). The default
# (no-profile / live-tap) path is untouched — it keeps mapping the fast
# ``_signal`` exactly as before.
_PICK_SLOW_ATTACK = 0.017  # ~1.2 s rise at 50 fps
_PICK_SLOW_DECAY = 0.006   # ~3.5 s fall at 50 fps
# Beats/second that reads as fully percussive (fast 16-note-ish groove).
_PICK_BEAT_FULL = 3.0
# Tempo term: BPM mapped across a ballad..club-techno span.
_PICK_BPM_LO = 85.0
_PICK_BPM_HI = 150.0
# Per-song "dynamics-honest" spread (only when a song intensity profile is
# supplied). ``dynamics`` is the song's own quiet..loud signal span (see
# ``trackmap.build_intensity_profile``): at/above this reference a song moves
# through its whole earned band; below it the moment is proportionally pulled
# back toward the (mood-shifted) middle of the band, so a flat, constant-loudness
# track sits still instead of twitching between rungs.
_PICK_DYN_REF = 0.26
# Cap on how far the mood term (spectral tilt + tempo) may slide the moment
# within the song's band: a moderate bias, never a takeover.
_PICK_MOOD_MAX = 0.16
# Shaping of the moment inside the song's earned band. >1 means the top of the
# band is a *peak*, not a plateau: the offline window's ceiling is a p95, so ~5%
# of every track sits at the very top of its own range and a linear map would
# hand the ceiling rung out that often. Mild — enough that the ceiling reads as
# a moment rather than a section.
_PICK_PEAK_GAMMA = 1.3

# --- Layer A: the absolute character score ----------------------------------
# Weights of the character terms (they sum to 1.0). Attack and busyness carry
# the most because they measure the thing most directly — how percussive and how
# constantly active the music is — and are the most robust in practice. Tempo is
# weighted lower on purpose: a half/double-time BPM lock is a real failure mode,
# so it must not be able to move a song a whole band on its own. Spectral tilt
# is lower still; it is the noisiest of the four (a bass-register drone reads as
# "heavy" on tilt alone, and only attack/busy tell them apart).
#
# NB busyness is measured from onset flux, NOT from beats-per-second. On a
# gridded map beats/second IS bpm/60 — it would just be the tempo term again,
# and a BPM octave error would then swing two terms at once.
#
# There is deliberately NO "how relentlessly loud is it" term. It sounds like it
# belongs, but ``energy`` is p95-normalised per track, so mean loudness measures
# sustained-vs-transient rather than intensity: a constant drone scores ~1.0 and
# a busy drum track (energy spiking between near-silent gaps) scores ~0.2. It
# ranks real music almost exactly backwards.
_CHAR_W_TEMPO = 0.20
_CHAR_W_BUSY = 0.32
_CHAR_W_ATTACK = 0.34
_CHAR_W_BASS = 0.14
# Onset broadbandness (``AnalysisFrame.onset_width``, energy-weighted mean over
# the track) spans roughly this range on real material: sustained/tonal content
# sits near zero, a full drum kit near the top. Measured off the analyser rather
# than assumed — the raw values are much lower than the per-frame width scale
# suggests, because most frames of any track are between transients.
_CHAR_ATTACK_LO = 0.04
_CHAR_ATTACK_HI = 0.30
# Mean onset flux that reads as constantly busy. Flux is p95-normalised per
# track, so its *mean* says how much of the song carries strong onsets: constant
# 16ths sit high, a sparse ballad spikes and falls back. Tempo-independent — a
# slow track with a busy groove scores busy, which is the point.
_CHAR_BUSY_FULL = 0.34
# Character assumed when the song is unknown — metadata-only playback, or the
# live estimator before it has warmed up. Mid-ladder and deliberately shy of the
# top, so an unidentified track opens around High and never on Extreme.
_CHAR_NEUTRAL = 0.50
# Live character estimator (no offline profile): EMA time-constant on the
# per-frame terms, how long before the estimate fully displaces _CHAR_NEUTRAL,
# and the energy below which a frame is too quiet to say anything about the song.
_CHAR_TAU_S = 12.0
_CHAR_WARMUP_S = 20.0
_CHAR_MIN_ENERGY = 0.15

# --- Layer B: character -> the ladder band it earns --------------------------
# Anchors on the 0..1 ladder axis (see ``_RUNG_SHARE`` for where each rung sits
# on it), linearly interpolated between. Deliberately non-linear at the top: only
# a genuinely heavy track's ceiling reaches into Extreme's cell, which is what
# makes Extreme rare rather than "the loudest 3% of literally every song".
_CHAR_BAND_ANCHORS = (
    # character  floor  ceiling      what it sounds like      ≈ rungs
    (0.00,       0.00,  0.16),     # ambient / drone          Subtle .. Medium
    (0.25,       0.03,  0.22),     # lofi / very chill        Subtle .. Medium
    (0.40,       0.14,  0.58),     # soft indie / acoustic    Medium .. High
    (0.60,       0.30,  0.86),     # pop / house              High   .. Intense
    (0.75,       0.34,  0.99),     # energetic dance          High   .. Intense
    (1.00,       0.40,  1.00),     # EDM / heavy              High   .. Extreme
)
# Note the floors rise much more slowly than the ceilings: a heavy track earns a
# high ceiling but must keep somewhere to *fall* to, or a breakdown has nowhere
# to go and the room sits on one rung for the whole song — especially under a
# narrow selection, where the cells are wide.

# --- Layer C: how wide each rung's cell is -----------------------------------
# Target share of playtime per rung, library-wide, ascending (Subtle..Extreme).
# The 0..1 ladder axis is divided in these proportions instead of a flat 1/n per
# rung, which is what encodes the intended feel: High and Intense carry the
# music, Medium/Subtle are for genuinely soft passages, and Extreme is a narrow
# spike at the very top. Renormalised over whatever subset the user enabled, so
# the relative prominence survives a narrower selection.
_RUNG_SHARE = (0.07, 0.16, 0.38, 0.36, 0.03)
# How far past a rung boundary's dead-band a trapped band is stretched (see
# ``_ensure_crossing``). Small on purpose: enough that the switch actually
# commits, not enough to turn the neighbouring rung into the song's home.
_BAND_CROSS_EPS = 0.01


def _intensity_signal(energy: float, salience: float, tempo: float, perc: float) -> float:
    """Blend live features into one 0..1 musical-intensity signal.

    Loudness is the gate: ``salience`` (how big this moment is versus the rest
    of the track — it spikes on drops/choruses) can only *amplify* a loud
    moment, never manufacture intensity out of a quiet one, so a silent intro
    can't trip Intense. Tempo and percussiveness add a modest steady-state lift
    so a fast, busy track sits a rung higher than a sparse one at equal loudness.
    """
    loud = max(0.0, min(1.0, energy))
    moment = loud * (0.55 + 0.45 * max(0.0, min(1.0, salience)))
    raw = 0.68 * moment + 0.16 * max(0.0, min(1.0, tempo)) + 0.16 * max(0.0, min(1.0, perc))
    return max(0.0, min(1.0, raw))


def _unit(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def song_character(tempo: float, busy: float, attack: float, bass: float) -> float:
    """Blend the absolute character terms into one 0..1 score for a song.

    Every term is chosen to survive the analysis' per-track p95 normalisation, so
    the score is comparable *across* songs — which is the whole point: a lofi
    track has to score low even though its own chorus normalises to 1.0 exactly
    like an EDM drop does. ``tempo`` is BPM over the ballad..club span, ``busy``
    how constantly onsets fire, ``attack`` their broadbandness (drums are
    broadband, pads and sung tones are not), ``bass`` the low-end weight.

    Shared by the offline profile (:func:`trackmap.build_intensity_profile`) and
    the picker's live estimator so a track scores the same either way.
    """
    return _unit(
        _CHAR_W_TEMPO * _unit(tempo)
        + _CHAR_W_BUSY * _unit(busy)
        + _CHAR_W_ATTACK * _unit(attack)
        + _CHAR_W_BASS * _unit(bass)
    )


def _character_band(character: float) -> tuple[float, float]:
    """The floor..ceiling of the 0..1 ladder axis a song of this character earns.

    Interpolated between :data:`_CHAR_BAND_ANCHORS`. This is Layer B: it decides
    how high a song *can* go before a single moment of it is looked at, so a
    chill track's biggest moment tops out at Medium while a banger's reaches
    Extreme. It also supplies the floor, which is why an energetic track never
    drops to Subtle without any explicit rule saying so.
    """
    c = _unit(character)
    for (c0, f0, t0), (c1, f1, t1) in zip(_CHAR_BAND_ANCHORS, _CHAR_BAND_ANCHORS[1:]):
        if c <= c1:
            w = 0.0 if c1 <= c0 else (c - c0) / (c1 - c0)
            return f0 + (f1 - f0) * w, t0 + (t1 - t0) * w
    return _CHAR_BAND_ANCHORS[-1][1], _CHAR_BAND_ANCHORS[-1][2]


def _rung_cells(rungs: list[SyncMode]) -> tuple[list[float], list[float]]:
    """Cell widths and interior edges of ``rungs`` on the 0..1 ladder axis.

    Each rung keeps its :data:`_RUNG_SHARE` of the axis, renormalised over the
    selection — so High stays the workhorse and Extreme stays a narrow spike
    whether you enabled three rungs or five. The axis this returns is therefore
    the SELECTION's axis, not the full ladder's; :func:`_to_selection` is what
    carries a full-ladder position onto it.
    """
    shares = [_RUNG_SHARE[INTENSITY_LADDER.index(m)] for m in rungs]
    total = sum(shares) or 1.0
    widths = [s / total for s in shares]
    edges: list[float] = []
    acc = 0.0
    for w in widths[:-1]:
        acc += w
        edges.append(acc)
    return edges, widths


def _bounds(widths: list[float]) -> list[float]:
    """Cumulative cell bounds ``[0, w0, w0+w1, ..., 1]`` for ``widths``."""
    out = [0.0]
    for w in widths:
        out.append(out[-1] + w)
    return out


@lru_cache(maxsize=64)
def _selection_knots(
    rungs: tuple[SyncMode, ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Knots of the full-ladder axis -> enabled-selection axis map.

    :data:`_CHAR_BAND_ANCHORS` are expressed on the FULL five-rung axis (a floor
    of 0.30 means "the bottom of High"), while :func:`_rung_cells` renormalises
    over the enabled set. Comparing one to the other directly is a category
    error: with High/Intense/Extreme enabled, High's cell starts at 0.0 and runs
    to 0.49, so every band whose full-ladder ceiling was below 0.49 collapsed
    onto the lowest enabled rung and the room sat there for the whole song.

    This builds the missing conversion: each enabled rung's full-ladder cell maps
    linearly onto its cell in the selection, and the disabled rungs' shares
    become the gaps between them. Monotone, and the identity when all five rungs
    are enabled, so a full selection behaves exactly as before.
    """
    full = _bounds(_rung_cells(list(INTENSITY_LADDER))[1])
    sel = _bounds(_rung_cells(list(rungs))[1])
    src: list[float] = []
    dst: list[float] = []
    for j, mode in enumerate(rungs):
        i = INTENSITY_LADDER.index(mode)
        for s, d in ((full[i], sel[j]), (full[i + 1], sel[j + 1])):
            if src and abs(s - src[-1]) < 1e-12:
                continue  # adjacent enabled rungs share a knot
            src.append(s)
            dst.append(d)
    return tuple(src), tuple(dst)  # cached: hand back something immutable


def _to_selection(
    pos: float, src: tuple[float, ...], dst: tuple[float, ...]
) -> float:
    """Carry ``pos`` from the full-ladder axis onto the enabled selection's axis.

    Positions below the lowest enabled rung land on the bottom of the selection
    and positions above the highest on its top — the sense in which the lowest
    enabled rung really is the floor.
    """
    if not src:
        return _unit(pos)
    if pos <= src[0]:
        return dst[0]
    for k in range(1, len(src)):
        if pos <= src[k]:
            span = src[k] - src[k - 1]
            if span <= 0.0:
                return dst[k]
            return dst[k - 1] + (dst[k] - dst[k - 1]) * (pos - src[k - 1]) / span
    return dst[-1]


def _ensure_crossing(
    floor: float, ceiling: float, edges: list[float], widths: list[float]
) -> tuple[float, float]:
    """Widen a band that is trapped inside a single cell until it can switch.

    A band that doesn't span the whole of some edge's dead-band — ``edge`` ±
    :data:`_PICK_HYST_FRAC` of the narrower neighbouring cell — can never move
    the room off one rung, whatever the song does. That is the "Auto sits on the
    lowest rung all song" fault: character can legitimately place a narrow band
    inside one cell, especially under a selection that omits the rungs the song
    would otherwise have used.

    So reach *just* past the nearest edge, both sides of its dead-band, and no
    further: the song's peak lifts it a rung and its breakdown drops it back,
    while the extra travel stays confined to those two adjacent rungs. A
    single-rung selection has no edge and is left alone.
    """
    if not edges:
        return floor, ceiling
    hyst = [_PICK_HYST_FRAC * min(widths[i], widths[i + 1]) for i in range(len(edges))]
    if any(floor <= e - h and ceiling >= e + h for e, h in zip(edges, hyst)):
        return floor, ceiling
    mid = 0.5 * (floor + ceiling)
    i = min(range(len(edges)), key=lambda k: abs(edges[k] - mid))
    return (
        max(0.0, min(floor, edges[i] - hyst[i] - _BAND_CROSS_EPS)),
        min(1.0, max(ceiling, edges[i] + hyst[i] + _BAND_CROSS_EPS)),
    )


class AutoIntensityPicker:
    """Resolve Auto to a concrete rung from what the music actually is.

    The song's absolute *character* (Layer A) decides the band of the ladder it
    earns (Layer B); its own section curve then moves within that band (Layer C).
    The enabled set (``allowed``) rescales the ladder axis rather than clipping
    it, so a heavy track reaches the top of your selection, a chill one stays
    low in it, and no song is forced to use every rung you enabled.

    Character comes from the offline profile when a track map is playing back;
    on a live tap the picker estimates the same terms itself over a warm-up
    window, starting from :data:`_CHAR_NEUTRAL` so an unidentified track opens
    around High and can't jump to Extreme on its first loud bar.

    A hysteresis dead-band proportional to the rung's cell and a long dwell floor
    keep switches slow and unhurried. This is purely a *selection* — it never
    changes how any rung renders.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Start fresh (session start). Seeds the signal mid-range so the room
        opens around the middle of the song's band, not ramping up from black."""
        self._signal = 0.56  # ≈ the middle of the real-music loudness window
        self._slow = 0.56    # section-level envelope (profile path only)
        self._beat_rate = 0.0
        # Live character terms, seeded neutral so the blend starts at
        # _CHAR_NEUTRAL before any music has been heard.
        self._char_attack = 0.5 * (_CHAR_ATTACK_LO + _CHAR_ATTACK_HI)
        self._char_busy = 0.5 * _CHAR_BUSY_FULL
        self._char_bass = 0.5
        self._char_age = 0.0  # seconds of actual music seen (warm-up progress)
        self._level: SyncMode | None = None  # emitted rung
        self._since_switch = _PICK_DWELL_S   # allow the first pick immediately

    @property
    def level(self) -> SyncMode | None:
        return self._level

    def allow_immediate_repick(self) -> None:
        """Clear the dwell so the next :meth:`update` may switch at once (used
        when the enabled set changes so a checklist toggle feels instant)."""
        self._since_switch = _PICK_DWELL_S

    def update(
        self,
        dt: float,
        *,
        energy: float,
        salience: float,
        bpm: float,
        beat: bool,
        allowed: tuple[SyncMode, ...],
        signal: float | None = None,
        lo: float = _SIG_LO_REF,
        hi: float = _SIG_HI_REF,
        dynamics: float | None = None,
        mood: float = 0.0,
        character: float | None = None,
        onset_width: float = 0.5,
        centroid: float = 0.5,
        flux: float = _CHAR_BUSY_FULL * 0.5,
    ) -> SyncMode:
        """Advance one frame and return the rung Auto should be at now.

        ``signal`` is the offline, lag-free section-intensity for this frame
        (from the map's :class:`IntensityProfile` curve). When given it is mapped
        DIRECTLY — no live smoothing — so the switch lands on the section change
        instead of a time-constant later. Without it (live tap / metadata) the
        picker smooths live as before.

        ``character`` is the song's absolute 0..1 energy character, which decides
        the band of the ladder it earns. Supplied by the offline profile; when
        omitted the picker estimates it live from ``onset_width``/``flux``/
        ``centroid`` and its own tempo/envelope state. ``lo``/``hi``/
        ``dynamics``/``mood`` are the song's intensity window and shading,
        defaulting to the fixed real-music window with no per-song shaping.
        """
        # Percussiveness: a leaky-integrator estimate of beats/second (decays
        # through quiet bridges, climbs on a busy groove). With time-constant
        # tau, adding 1/tau per beat and bleeding rate*dt/tau per frame settles
        # at the true beats/second for a steady groove.
        tau = 1.5
        self._beat_rate *= max(0.0, 1.0 - dt / tau)
        if beat:
            self._beat_rate += 1.0 / tau
        perc = max(0.0, min(1.0, self._beat_rate / _PICK_BEAT_FULL))
        tempo = (
            max(0.0, min(1.0, (bpm - _PICK_BPM_LO) / (_PICK_BPM_HI - _PICK_BPM_LO)))
            if bpm > 0.0 else 0.5
        )
        raw = _intensity_signal(energy, salience, tempo, perc)
        alpha = _PICK_ATTACK if raw > self._signal else _PICK_DECAY
        self._signal += (raw - self._signal) * alpha
        slow_alpha = _PICK_SLOW_ATTACK if raw > self._slow else _PICK_SLOW_DECAY
        self._slow += (raw - self._slow) * slow_alpha
        self._since_switch += dt

        # Live character terms, tracked only while music is actually playing so a
        # silent intro or a gap between tracks can't drag the estimate down.
        if character is None and energy >= _CHAR_MIN_ENERGY:
            ema = min(1.0, dt / _CHAR_TAU_S)
            self._char_attack += (onset_width - self._char_attack) * ema
            self._char_busy += (flux - self._char_busy) * ema
            self._char_bass += ((1.0 - centroid) - self._char_bass) * ema
            self._char_age += dt
        char = self._character(tempo) if character is None else _unit(character)

        # Prefer the offline lag-free curve (map playback) — mapped directly so
        # the pick follows the song's real section arc with no envelope lag. Fall
        # back to the live section envelope when a profile is active without a
        # curve, and to the fast signal on the live-tap / no-profile path.
        if signal is not None:
            sig = max(0.0, min(1.0, signal))
        elif dynamics is not None:
            sig = self._slow
        else:
            sig = self._signal
        target = self._resolve(sig, allowed, lo, hi, dynamics, mood, char)
        if self._level is None:
            self._level = target  # first frame: adopt without waiting on dwell
        elif target is not self._level:
            # Big energy changes (a drop/breakdown) commit fast so the switch lands
            # on the section; small one-rung nudges keep the long, unhurried dwell.
            gap = abs(INTENSITY_LADDER.index(target) - INTENSITY_LADDER.index(self._level))
            dwell = _PICK_BIG_DWELL_S if gap >= _PICK_BIG_JUMP else _PICK_DWELL_S
            if self._since_switch >= dwell:
                self._level = target
                self._since_switch = 0.0
        return self._level

    def _character(self, tempo: float) -> float:
        """The live character estimate, eased in from neutral over the warm-up.

        Same terms as the offline profile, tracked off the live frames: BPM and
        the smoothed onset flux, broadbandness and spectral tilt. Blended toward
        :data:`_CHAR_NEUTRAL` until ``_CHAR_WARMUP_S`` of music has been heard, so
        the opening bars of an unknown track can't earn it a high ceiling.
        """
        raw = song_character(
            tempo=tempo,
            busy=self._char_busy / _CHAR_BUSY_FULL,
            attack=(self._char_attack - _CHAR_ATTACK_LO)
            / (_CHAR_ATTACK_HI - _CHAR_ATTACK_LO),
            bass=self._char_bass,
        )
        w = _unit(self._char_age / _CHAR_WARMUP_S)
        return _CHAR_NEUTRAL + (raw - _CHAR_NEUTRAL) * w

    def _resolve(
        self,
        signal: float,
        allowed: tuple[SyncMode, ...],
        lo: float = _SIG_LO_REF,
        hi: float = _SIG_HI_REF,
        dynamics: float | None = None,
        mood: float = 0.0,
        character: float = _CHAR_NEUTRAL,
    ) -> SyncMode:
        """Place this moment on the ladder, then on the enabled set, with hysteresis.

        1. Where the moment sits *in the song*: the signal normalised against the
           loudness window ``lo..hi`` (the song's own quiet..loud span when a
           profile is supplied, else the fixed real-music window), compressed
           toward the middle when the song is **dynamics-honest** flat, shaded by
           ``mood``, and gamma-shaped so the top of the band reads as a peak.
        2. Where that lands *on the ladder*: scaled into the floor..ceiling band
           the song's ``character`` earned. A chill song's band tops out around
           Medium however loud its own chorus is — the fix for Auto reaching
           Intense/Extreme on music that doesn't call for it.
        3. Which enabled rung that is: the band is carried from the full ladder
           onto the selection's axis (:func:`_to_selection`), whose cells are
           :data:`_RUNG_SHARE` renormalised over the enabled set, so High stays
           the workhorse and Extreme a narrow spike at any selection size. A band
           that lands inside a single cell is widened just enough to reach the
           nearest boundary (:func:`_ensure_crossing`), so the song's arc always
           has somewhere to go. The dead-band on each edge scales with the cell,
           so switches stay stable without making a narrow cell unreachable.
        """
        rungs = sorted(
            (m for m in allowed if m in INTENSITY_LADDER), key=INTENSITY_LADDER.index
        ) or list(DEFAULT_AUTO_LEVELS)
        n = len(rungs)
        span = max(1e-3, hi - lo)
        p = _unit((signal - lo) / span)
        if dynamics is not None:
            # How much of its band this song has earned (0 flat .. 1 dynamic).
            w = _unit(dynamics / _PICK_DYN_REF)
            p = 0.5 + w * (p - 0.5)
        p = _unit(p + max(-_PICK_MOOD_MAX, min(_PICK_MOOD_MAX, mood)))
        edges, widths = _rung_cells(rungs)
        # The band character earned is on the FULL ladder; carry it onto the
        # selection's axis before it meets the selection's cells, then make sure
        # it can actually reach a boundary — a band trapped inside one cell is a
        # room that never moves, however dynamic the song is.
        src, dst = _selection_knots(tuple(rungs))
        floor, ceiling = _character_band(character)
        floor, ceiling = _ensure_crossing(
            _to_selection(floor, src, dst), _to_selection(ceiling, src, dst),
            edges, widths,
        )
        pos = floor + (ceiling - floor) * (p ** _PICK_PEAK_GAMMA)

        if self._level in rungs:
            b = rungs.index(self._level)
        else:
            b = 0
            while b < n - 1 and pos >= edges[b]:
                b += 1
        while b < n - 1 and pos > edges[b] + _PICK_HYST_FRAC * min(widths[b], widths[b + 1]):
            b += 1
        while b > 0 and pos < edges[b - 1] - _PICK_HYST_FRAC * min(widths[b - 1], widths[b]):
            b -= 1
        return rungs[b]


def sanitize_auto_levels(levels) -> tuple[SyncMode, ...]:
    """Normalise a user-supplied enabled set to valid, ordered, non-empty rungs.

    Accepts SyncMode or str members, drops Auto and anything unknown, dedupes,
    orders by the ladder, and falls back to the default set when nothing valid
    remains — so a bad or empty selection can never leave Auto with no rung.
    """
    out: list[SyncMode] = []
    for item in levels or ():
        try:
            m = SyncMode(item)
        except ValueError:
            continue
        if m in INTENSITY_LADDER and m not in out:
            out.append(m)
    out.sort(key=INTENSITY_LADDER.index)
    return tuple(out) if out else tuple(DEFAULT_AUTO_LEVELS)


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

# Instrument roles a light can hold.
ROLE_BASS = 0  # rides the bass envelope, snaps on kicks
ROLE_MID = 1   # rides the mids, pops on guitar/snare onsets
ROLE_VOCAL = 2  # very dim, shimmers with singing / high content


def _role_counts(count: int, mix: tuple[float, float, float]) -> tuple[int, int, int]:
    """How many (bass, mid, vocal) lights for ``count`` lamps and a role ``mix``.

    Largest-remainder (Hamilton) allocation: floor each share, then hand the
    leftover lamps to the biggest fractional remainders (bass wins ties), so the
    counts track the proportion cleanly as the light count changes instead of
    each role rounding on its own. Every role with a non-zero share is then
    guaranteed at least one lamp once there are enough to go round, and a mix
    that wants no vocal hands its spares back to bass.

    A Hue entertainment area holds at most 10 lamps, so the High split
    ``(0.4, 0.3, 0.3)`` resolves to this per-count ladder (bass keeps the
    plurality, so the kick is always well represented):

        N : bass mid vocal        N : bass mid vocal
        1 :  1    0    0           6 :  2    2    2
        2 :  1    1    0           7 :  3    2    2
        3 :  1    1    1           8 :  3    3    2
        4 :  2    1    1           9 :  3    3    3
        5 :  2    2    1          10 :  4    3    3
    """
    raw = [mix[0] * count, mix[1] * count, mix[2] * count]
    counts = [int(math.floor(r)) for r in raw]
    remaining = count - sum(counts)
    # Give the spare lamps to the largest fractional parts; on a tie prefer the
    # earlier role (bass, then mid), so bass keeps its edge.
    order = sorted(range(3), key=lambda i: (raw[i] - counts[i], -i), reverse=True)
    for i in order[:max(0, remaining)]:
        counts[i] += 1
    # Guarantee a lamp for each wanted role once the room is big enough, pulling
    # the extra from the most over-allocated role (always bass for these mixes).
    for idx, want in ((1, mix[1] > 0.0 and count >= 2), (2, mix[2] > 0.0 and count >= 3)):
        if want and counts[idx] == 0:
            donor = max(range(3), key=lambda i: counts[i])
            if counts[donor] > 1:
                counts[donor] -= 1
                counts[idx] += 1
    nb, nm, nv = counts
    if mix[2] <= 0.0 and nv > 0:  # no vocal role wanted: hand spares to bass
        nb += nv
        nv = 0
    if mix[1] <= 0.0 and nm > 0:  # no mid role wanted: hand spares to bass
        nb += nm
        nm = 0
    return nb, nm, nv


def _interleave(counts: tuple[int, int, int]) -> list[int]:
    """Spread the role lamps evenly across the ranks instead of in blocks.

    Each role's lamps are placed at fractional positions ``(j + 0.5) / count``
    along the room and all positions are merged in order (bass winning ties), so
    e.g. (3, 3, 2) -> [B, M, V, B, M, V, B, M] with the bass lamps at ranks
    0/3/6 rather than clustered together. That way the lamps that snap on the
    kick are distributed around the room, not bunched on one side.
    """
    slots: list[tuple[float, int, int]] = []
    for role, c in ((ROLE_BASS, counts[0]), (ROLE_MID, counts[1]), (ROLE_VOCAL, counts[2])):
        for j in range(c):
            slots.append(((j + 0.5) / c, role, role))
    slots.sort(key=lambda s: (s[0], s[1]))
    return [role for _pos, _prio, role in slots]


def assign_roles(count: int, mix: tuple[float, float, float], offset: int) -> list[int]:
    """Role per light rank (left-to-right), rotated by ``offset``.

    ``mix`` gives the (bass, mid, vocal) fractions; counts scale cleanly with the
    light count (see :func:`_role_counts`), the roles are spread evenly around the
    room rather than clustered (see :func:`_interleave`), and rotation cycles the
    assignment so the "band members" trade places.
    """
    if count <= 0:
        return []
    roles = _interleave(_role_counts(count, mix))
    off = offset % count
    return roles[-off:] + roles[:-off] if off else roles


def band_for_rank(rank: int, count: int) -> str:
    """Assign a frequency band to a channel given its left-to-right rank."""
    if count <= 1:
        return "bass"
    idx = int(rank / count * len(_BAND_ORDER))
    return _BAND_ORDER[min(idx, len(_BAND_ORDER) - 1)]


def _shimmer(t: float, cid: int) -> float:
    """Fast, per-channel pseudo-random sparkle in 0..1."""
    return 0.5 + 0.5 * math.sin(t * 23.0 + cid * 2.7) * math.sin(t * 8.0 + cid * 1.3)


# Below this normalised broadband energy the room is treated as effectively
# silent: the continuous melbank lift fades out so a paused/quiet track rests
# in darkness instead of glowing at the ambient floor.
_MUSIC_GATE = 0.12


def _pan_weighted_mean(
    values,
    pan,
    lo: int,
    hi: int,
    side: float,
    gain: float,
    peakiness: float = 0.0,
    mel_w: list[float] | None = None,
) -> float:
    """Mean of ``values[lo:hi]`` with each bin weighted toward this lamp's
    side of the stereo field: a bin panned to the lamp's side counts up to
    double, one panned away fades toward zero. Dividing by the bin count
    (not the weight sum) keeps a centred mix EXACTLY the unweighted mean —
    hard pans redistribute brightness across the room, never add to it.

    ``mel_w`` (P3) optionally applies the per-bin absolute-loudness weights;
    ``peakiness`` (P3, from CAMusic) blends the weighted mean toward the
    weighted hottest bin — a held vocal occupies two or three of a lamp's
    ~seven bins, so a pure mean delivered it at roughly a third of its real
    height — audible as a mid that is present in the music and absent from
    the room. Defaults keep the plain weighted mean.
    """
    total = 0.0
    peak = 0.0
    for k in range(lo, hi):
        w = 1.0 + gain * pan[k] * side
        if w < 0.0:
            w = 0.0
        elif w > 2.0:
            w = 2.0
        v = values[k] * w
        if mel_w is not None and k < len(mel_w):
            v *= mel_w[k]
        total += v
        if v > peak:
            peak = v
    mean = total / (hi - lo)
    if peakiness <= 0.0:
        return mean
    return (1.0 - peakiness) * mean + peakiness * peak


def _melbank_drive(
    frame,
    env: dict[str, float],
    info: dict,
    pan_gain: float = 0.0,
    peakiness: float = 0.0,
    mel_w: list[float] | None = None,
) -> float:
    """This lamp's continuous reactive level (0..~1) from its melbank slice.

    Falls back to the lamp's coarse band envelope when the analyzer did not
    populate a melbank (e.g. unit-test frames), so the continuous layer is
    always defined and the room never goes dark purely for lack of a melbank.
    With stereo pan available, the slice is weighted toward the lamp's side
    of the stereo field so panned instruments light the matching side.
    ``peakiness`` (P3, from CAMusic) blends the slice's mean toward its
    hottest bin; ``mel_w`` (P3) applies per-bin absolute-loudness weights.
    """
    mel = getattr(frame, "melbank", None)
    if mel:
        lo, hi = info["mel_lo"], info["mel_hi"]
        if hi > lo:
            pan = getattr(frame, "pan", None)
            if pan_gain > 0.0 and pan and len(pan) >= hi:
                side = 2.0 * info["nx"] - 1.0
                return _pan_weighted_mean(
                    mel, pan, lo, hi, side, pan_gain, peakiness, mel_w
                )
            if peakiness <= 0.0 and mel_w is None:
                seg = mel[lo:hi]
                return sum(seg) / len(seg)
            vals = [
                v
                * (mel_w[lo + i] if mel_w and lo + i < len(mel_w) else 1.0)
                for i, v in enumerate(mel[lo:hi])
            ]
            mean = sum(vals) / len(vals)
            if peakiness <= 0.0:
                return mean
            return (1.0 - peakiness) * mean + peakiness * max(vals)
    return env.get(
        info["band"], max(env.get("bass", 0.0), env.get("sub_bass", 0.0))
    )


def beat_colour_advance(params: ModeParams, strength: float, bass: float) -> float:
    """Extra palette phase to add for a *visible* beat event.

    This is what makes the colour *move with the music* rather than only drifting
    on a timer: every visible beat nudges the whole palette forward, weighted by
    how strong it is and how much bass it carries, so the colour steps on the
    kick. The engine decides which onsets qualify (bass onsets, on-grid when the
    tempo is locked); any qualifying beat counts (not just the big
    ``beat_threshold`` ones) so the colour keeps grooving in quieter sections.
    """
    if strength <= 0.0 or params.colour_beat_step <= 0.0:
        return 0.0
    weight = 0.5 + 0.5 * bass
    return params.colour_beat_step * min(1.5, 0.5 + strength) * weight


# Musical pulse hierarchy across the bar: the downbeat hits hardest, beat 3
# carries, beats 2/4 land softer — the 1:1-but-musical pulse of the references.
_BAR_W = (1.0, 0.72, 0.86, 0.72)


# A ranked highlight never lands limp: selective modes pulse it at least this
# hard even when the passage is quiet and its absolute accent is small.
_HL_MIN = 0.55


def pulse_weight(
    p: ModeParams, accent: float, beat_in_bar: int, highlight: bool = True
) -> float:
    """0..1 size of a scheduled beat pulse from its accent and bar position.

    ``highlight`` is the engine's rank-based selection (top accents of the
    recent passage): highlights pulse at full musical size — selective modes
    guarantee at least ``_HL_MIN`` so a ranked beat never lands limp — while
    non-highlights get only ``weak_pulse``, the quiet metronome between hits
    (zero in Extreme: ordinary beats stay dark). ``accent_floor`` shapes the
    response *within* highlights, and ``downbeat_pulse`` guarantees the bar's
    "one" still lands either way, so the room never loses the pulse.
    """
    if highlight:
        a = (accent - p.accent_floor) / max(1e-6, 1.0 - p.accent_floor)
        a = max(0.0, min(1.0, a))
        if p.highlight_quantile > 0.0:
            a = max(a, _HL_MIN)
        w = p.weak_pulse + (1.0 - p.weak_pulse) * a
    else:
        w = p.weak_pulse
    if beat_in_bar == 0:
        w = max(w, p.downbeat_pulse)
    return w * _BAR_W[beat_in_bar % 4]


def beat_pulse(
    p: ModeParams, accent: float, beat_in_bar: int, bass: float, highlight: bool = True
) -> float:
    """Snap a bass-role light gets from a *scheduled* (grid-locked) beat."""
    if p.beat_gain <= 0.0:
        return 0.0
    return (
        p.beat_gain
        * pulse_weight(p, accent, beat_in_bar, highlight)
        * (0.6 + 0.4 * bass)
    )


def event_gates(p: ModeParams, salience: float, width: float) -> tuple[float, float]:
    """(amplitude_scale, width_gate) for a detected onset under this mode.

    The two precision gates of the event-selection upgrade. ``amplitude_scale``
    makes every reaction proportional to the frame's ABSOLUTE loudness within
    the track (never amplifies — salience saturates at 1): the quiet-intro
    pluck pulses small, the drop slams. ``width_gate`` mutes detected onsets
    whose flux is too narrowband to be percussion (sung vowels, sustained
    tones) through a smoothstep knee so borderline onsets fade rather than
    flicker. Scheduled grid beats are amplitude-scaled but never width-gated —
    they were verified by the tempo model, not by this frame's spectrum.
    """
    s = max(0.0, min(1.0, salience))
    amp = max(p.salience_floor, s ** p.salience_gamma)
    w = (width - p.width_min) / max(1e-6, p.width_soft)
    w = max(0.0, min(1.0, w))
    return amp, w * w * (3.0 - 2.0 * w)


def accent_knee(strength: float, threshold: float) -> float:
    """Continuous accent gate: 0 below (thr−0.4), 1 above (thr+0.4).

    Binary thresholds are why beats felt random: the adaptive onset threshold
    tracks the kicks themselves, so identical-sounding kicks score ~1.0–1.6 and
    a hard gate passes an arbitrary subset. A soft knee makes every beat's
    response proportional to how hard the song actually hit it — strong accents
    slam, ordinary beats give a smaller swell, weak ones fade out smoothly.
    """
    return max(0.0, min(1.0, (strength - threshold + 0.4) / 0.8))


def kick_flash(params: ModeParams, strength: float, bass: float) -> float:
    """Snap a bass-role light gets from a kick, scaled by its accent.

    Weighted by bass content so the snaps track the kick rather than incidental
    onsets. The engine keeps this as a fast-decaying per-light overlay so beats
    snap to full independent of the slower continuous smoothing.
    """
    if strength <= 0.0:
        return 0.0
    # Full on kicks, dimmer on bass-less onsets; the floor is per-mode.
    weight = params.kick_bass_floor + (1.0 - params.kick_bass_floor) * bass
    return (
        params.beat_gain
        * min(1.0, strength / 2.0)
        * weight
        * accent_knee(strength, params.beat_threshold)
    )


def mid_flash(params: ModeParams, strength: float) -> float:
    """Pop a mid-role light gets from a guitar/snare onset, accent-scaled."""
    if params.mid_gain <= 0.0 or strength <= 0.0:
        return 0.0
    return (
        params.mid_gain
        * min(1.0, strength / 2.0)
        * accent_knee(strength, params.mid_threshold)
    )


def render(engine, frame) -> dict[int, tuple[RGB, float]]:
    """Per-channel (colour, continuous brightness) — no beat flash (added later).

    Continuous contributions read the engine's asymmetric band envelope
    followers (snap up on energy, decay gently) rather than the raw per-frame
    band values, so brightness *moves with* the music instead of jittering.
    When the mode splits the room into instrument roles, each light rides the
    envelope of *its* instrument: bass lights follow the bass, mid lights the
    guitar range, vocal lights shimmer dimly with the singing.
    """
    p: ModeParams = engine.active_params
    t = engine.time
    env = engine.band_env
    bass = max(env.get("sub_bass", 0.0), env.get("bass", 0.0))
    mids = max(env.get("low_mid", 0.0), env.get("mid", 0.0))
    treble = env.get("high", 0.0)
    vocal_drive = max(treble, 0.6 * env.get("low_mid", 0.0))
    roles = engine.roles
    has_roles = p.role_mix[1] > 0.0 or p.role_mix[2] > 0.0
    # Track-section arc (1.0 when no map): quiet sections dim the base, soften
    # the waves and tighten the colour spread so the chorus visibly opens up.
    # Only the head-room above the floor is scaled, so a "no dimming" mode
    # (base == floor) holds perfectly steady through the whole song.
    lvl = engine.section_level
    base_mul = 0.65 + 0.35 * lvl
    env_mul = 0.6 + 0.4 * lvl
    wave_mul = 0.5 + 0.5 * lvl
    span = 0.4 + 0.6 * lvl
    base_term = p.floor + (p.base - p.floor) * base_mul
    # The LedFx continuous layer fades in with loudness so silence rests dark.
    music = frame.energy / _MUSIC_GATE
    music = 0.0 if music < 0.0 else 1.0 if music > 1.0 else music
    # Rotating modes also reseed the colour layout on each rotation (an
    # irrational-ish step so the arrangement never repeats), so the room's
    # colour geography moves with the band.
    rot = 0.37 * engine.role_offset if p.role_rotate_beats > 0 else 0.0

    waves = engine.active_waves
    tr = engine.mel_transient  # per-bin spectral transients (all-instrument pops)
    # Per-bin absolute-loudness weights (P3, from CAMusic), computed once for
    # the frame: this used to be reached only by Extreme's renderer, which is
    # why the ``loudness`` tunable did nothing on any other rung.
    mel_w = engine.melbank_loud_weights(frame, p)
    out: dict[int, tuple[RGB, float]] = {}
    for ch in engine.channels:
        info = engine.cmap[ch.channel_id]
        role = roles.get(ch.channel_id, ROLE_BASS)
        # EVERY lamp gets a strong continuous reaction: the low-end weight of its
        # role drive PLUS its own slice of the melbank spectrum PLUS the room
        # loudness. No lamp is ever starved - the whole room reacts to the music.
        # Roles only add *flavour* on top (kick/guitar punch, vocal shimmer);
        # they no longer decide whether a lamp reacts at all.
        drive = mids if (has_roles and role == ROLE_MID) else bass
        bri = base_term + p.bass_gain * drive * env_mul
        if p.melbank_gain:
            mel_drive = _melbank_drive(
                frame, env, info, p.pan_gain, p.mel_peakiness, mel_w
            )
            bri += (p.melbank_floor + p.melbank_gain * mel_drive) * music * env_mul
        if p.energy_gain:
            # The whole room follows the song's loudness contour together (the
            # "brighten on the build, dim in the breakdown" motion).
            bri += p.energy_gain * engine.energy_env
        if p.spectral_pop and tr:
            # Pop on a fresh attack anywhere in this lamp's slice of the spectrum
            # (kick -> low lamps, snare -> low-mids, guitar -> mids, cymbal -> highs),
            # pan-weighted so a panned hit pops the matching side of the room.
            lo, hi = info["mel_lo"], info["mel_hi"]
            if hi > lo:
                pan = getattr(frame, "pan", None)
                if p.pan_gain > 0.0 and pan and len(pan) >= hi:
                    side = 2.0 * info["nx"] - 1.0
                    pop = _pan_weighted_mean(
                        tr, pan, lo, hi, side, p.pan_gain, p.mel_peakiness, mel_w
                    )
                elif p.mel_peakiness > 0.0 or mel_w is not None:
                    # Same mean-and-peak blend as the glow drive, for the same
                    # reason: a single-bin transient averaged over a lamp's
                    # whole slice arrives at a fraction of its size.
                    vals = [
                        v
                        * (mel_w[lo + i] if mel_w and lo + i < len(mel_w) else 1.0)
                        for i, v in enumerate(tr[lo:hi])
                    ]
                    mean = sum(vals) / len(vals)
                    pop = (
                        (1.0 - p.mel_peakiness) * mean
                        + p.mel_peakiness * max(vals)
                        if vals
                        else 0.0
                    )
                else:
                    pop = sum(tr[lo:hi]) / (hi - lo)
                bri += p.spectral_pop * pop * music
        if has_roles and role == ROLE_VOCAL:
            # The human flavour: a vocal lamp still reacts to the music (above),
            # then shimmers with the singing on top - softened a touch, but never
            # the dim, starved layer it used to be.
            bri = 0.75 * bri + p.shimmer * vocal_drive * _shimmer(t, ch.channel_id)
        if p.tonal_gain > 0.0:
            # Sustain bloom (P3): ONE room-wide value, so a long vocal lifts
            # every lamp together into a single glow rather than nudging
            # whichever lamp happens to own that slice of the spectrum. A
            # ``bri`` addend, deliberately not part of the per-beat flash: it
            # goes through bri_attack/bri_decay and the per-second rate caps,
            # so its per-frame change stays far under FLASH_DELTA — a smooth
            # gradation invisible to both the WCAG limiter and the 12.5 Hz
            # rate limiter (from CAMusic's render loop).
            bri += p.tonal_gain * engine.tonal_env()
        if p.spread:
            bri += p.spread * env.get(info["band"], 0.0)
        if p.height_freq:
            # Lamps high in the room favour treble, low lamps favour bass.
            bri += p.height_freq * env.get(info["hband"], 0.0)
        if p.depth_wash:
            # Back/far lamps carry a gentle ambient wash; front lamps stay reactive.
            bri += p.depth_wash * (1.0 - info["ny"])
        if p.wave_gain and waves:
            # Beat wavefront(s) sweeping out from each wave's own origin (the
            # phrase cycle moves it around the room). Vocal lights only catch
            # a fraction, keeping their quiet identity.
            dists = info["dist_origins"]
            amp = 0.0
            for w in waves:
                amp += w.amplitude_at(dists[w.origin_idx])
            part = 0.4 if (has_roles and role == ROLE_VOCAL) else 1.0
            bri += p.wave_gain * wave_mul * part * amp
        if p.shimmer and not has_roles:
            # Role-less modes keep the classic everywhere-sparkle.
            bri += p.shimmer * treble * _shimmer(t, ch.channel_id)

        bri = p.floor if bri < p.floor else 1.0 if bri > 1.0 else bri
        # Palette position = this light's spatial rank (compressed in quiet
        # sections) + the engine's accumulated colour phase (time drift +
        # highlight steps), so colour moves with the music. colour_distribute
        # morphs the spatial gradient toward golden-ratio spacing by rank —
        # every lamp its own distinct hue (the apartment-sync look) instead of
        # near-neighbours on a smooth gradient.
        cpos = info["xrank"] * span * p.colour_spread
        colour = engine.palette.sample(cpos + rot + engine.colour_phase)
        # Theme-faithful value: a dark palette swatch (dark silver, deep purple)
        # renders as dimmer light, so moody album art gives a moody show. The
        # engine's chroma pipeline renormalises colour, so the value must be
        # folded into brightness here. Full-value palettes are unaffected.
        cval = max(colour)
        bri *= 0.35 + 0.65 * cval
        out[ch.channel_id] = (colour, bri)

    return out
