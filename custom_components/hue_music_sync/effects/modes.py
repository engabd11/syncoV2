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
    bri_slew: float = 1.0      # max emitted brightness RISE per frame: 1.0 =
    #                            instant snap (a strobe); lower turns each beat
    #                            into a fast dim<->bright SWING (≈full in
    #                            1/bri_slew frames, e.g. 0.22 ≈ 90 ms at 50 fps).
    #                            Falls are unlimited, so the room still dims fast.
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
    ),
    # Gentle club: visible dimming, soft flashes on the stronger beats, album
    # colours stepping each beat across a wide spatial spread. The calmest of
    # the colour-jump modes.
    SyncMode.MEDIUM: ModeParams(
        base=0.12, floor=0.05, bass_gain=0.14, beat_gain=0.9, beat_threshold=1.4,
        spread=0.0, colour_speed=0.05, shimmer=0.10, colour_sat=0.7,
        colour_beat_step=0.0, colour_lerp=0.40, bri_attack=1.0, bri_decay=0.30,
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
        wave_gain=0.55, wave_speed=2.2, wave_width=0.32,
        anticipation_ms=80, drop_boost=0.60, build_desat=0.45,
        role_mix=(0.4, 0.3, 0.3), mid_gain=1.0, mid_threshold=1.25,
        vocal_dim=0.05, role_rotate_beats=16, dynamic_roles=True, hard_snap=True,
        flash_decay=0.80, bri_slew=0.30,
        highlight_quantile=0.40, weak_pulse=0.16, downbeat_pulse=0.45,
        colour_jump=0.09, colour_spread=0.55, full_room_accent=0.94,
        energy_gain=0.15,
        melbank_gain=0.44, melbank_floor=0.035, colour_flow=0.05, spectral_pop=0.45,
        salience_gamma=1.0, width_min=0.12, kick_bass_floor=0.35,
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
        bri_slew=0.22, flash_decay=0.82,
        wave_gain=0.55, wave_speed=2.4, wave_width=0.30,
        anticipation_ms=90, drop_boost=0.80, build_desat=0.50,
        role_mix=(1.0, 0.0, 0.0), hard_snap=True,
        highlight_quantile=0.18, weak_pulse=0.42, downbeat_pulse=0.55,
        colour_jump=0.16, colour_spread=0.22, full_room_accent=0.0,
        melbank_gain=0.42, melbank_floor=0.06, colour_flow=0.05, spectral_pop=0.45,
        salience_gamma=0.8, width_min=0.08, nobeat_flash=0.30,
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
        spectral_pop=1.8,                         # PEAK FLASH: ∝ band attack height
        mel_flux_gain=1.25,                       # groove: every real hit re-fires, not just novel peaks
        mel_flux_floor=0.12,                      # but ignore ambient/noise wash — only real attacks
        rotate_rate=0.16,                         # spectrum rotates ~one lamp every 6 s
        energy_gain=0.06,                         # a touch of whole-room loudness lift (kept low)
        flash_decay=0.70,                         # per-frame fade of a peak flash
        bri_attack=0.5, bri_decay=0.4,            # glow smoothing (flash stays sharp)
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
# The intensity signal is mapped across the ENABLED set: the loud parts of a
# song reach the highest enabled rung and the quiet parts sit on the lowest, so
# the range you pick is the range you get (add Intense/Extreme and they get
# used; make High the lowest and it becomes the floor). BPM/mood still decide
# *where* in that range a moment lands, via the signal itself.
#
# The signal is first put on a 0..1 scale against the loudness window real music
# actually occupies (near-silence .. a full drop) so every enabled rung is
# reachable regardless of how many are enabled — a fast, busy track simply rides
# higher in the band than a sparse one.
_SIG_LO_REF = 0.25   # ~a quiet intro maps to the bottom of the enabled range
_SIG_HI_REF = 0.88   # ~a full drop maps to the top of it
# Anti-flicker dead-band on a band boundary (on the 0..1 normalised scale), a
# floor on seconds between committed switches (kept long so a switch is never
# rushed and the new rung has time to breathe), and the asymmetric smoothing on
# the raw signal: rise reasonably quick so a drop is caught, fall slow so a
# brief dip doesn't drop the room out of the chorus.
_PICK_HYST = 0.07
_PICK_DWELL_S = 3.5
_PICK_ATTACK = 0.10   # per-frame EMA weight while the signal is rising
_PICK_DECAY = 0.03    # per-frame EMA weight while it is falling
# Section-level smoothing used ONLY on the per-song-profile path: rung selection
# should follow the song's *sections* (verse↔chorus↔drop), not individual beats.
# Stretching a song's own quiet↔loud across the enabled set amplifies beat-to-
# beat pumping, so mapping a much slower envelope keeps switches unhurried while
# still reaching every rung on real section changes. Asymmetric like the fast
# one (rise in ~1.2 s to catch a drop, fall over ~3.5 s so a brief dip doesn't
# leave the chorus). The default (no-profile / live-tap) path is untouched — it
# keeps mapping the fast ``_signal`` exactly as before.
_PICK_SLOW_ATTACK = 0.017  # ~1.2 s rise at 50 fps
_PICK_SLOW_DECAY = 0.006   # ~3.5 s fall at 50 fps
# Beats/second that reads as fully percussive (fast 16-note-ish groove).
_PICK_BEAT_FULL = 3.0
# Tempo term: BPM mapped across a ballad..club-techno span.
_PICK_BPM_LO = 85.0
_PICK_BPM_HI = 150.0
# Per-song "dynamics-honest" spread (only when a song intensity profile is
# supplied). ``dynamics`` is the song's own quiet..loud signal span (see
# ``trackmap.build_intensity_profile``): at/above this reference a song is
# treated as fully dynamic and its quiet↔loud is stretched across the WHOLE
# enabled range (so every selected rung gets used); below it the spread is
# proportionally pulled back toward the (mood-shifted) centre so a flat,
# constant-loudness track sits still instead of twitching between rungs. Kept
# low so ordinary songs (which do breathe verse↔chorus) reach full spread.
_PICK_DYN_REF = 0.26
# Cap on how far the mood term (spectral tilt + tempo) may slide the operating
# point away from centre: a moderate bias, never a takeover.
_PICK_MOOD_MAX = 0.16


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


class AutoIntensityPicker:
    """Resolve Auto to a concrete rung from the music, spread over an enabled set.

    Fed the live per-frame features, it maintains a smoothed intensity signal and
    maps it ACROSS the rungs the user enabled (``allowed``): the quietest parts
    sit on the lowest enabled rung and the biggest moments reach the highest, so
    enabling Intense/Extreme really does put them on the loud parts, and making
    High the lowest makes High the floor. BPM/mood shape the signal, so they
    decide where in that range each moment lands. A wide hysteresis dead-band and
    a long dwell floor keep switches slow and unhurried. This is purely a
    *selection* — it never changes how any rung renders.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Start fresh (session start). Seeds the signal mid-range so the room
        opens around the middle of the enabled range, not ramping up from black."""
        self._signal = 0.56  # ≈ the middle of the real-music loudness window
        self._slow = 0.56    # section-level envelope (profile path only)
        self._beat_rate = 0.0
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
        lo: float = _SIG_LO_REF,
        hi: float = _SIG_HI_REF,
        dynamics: float | None = None,
        mood: float = 0.0,
    ) -> SyncMode:
        """Advance one frame and return the rung Auto should be at now.

        ``lo``/``hi``/``dynamics``/``mood`` are the current song's intensity
        profile (from :func:`trackmap.build_intensity_profile`, carried on the
        frame): ``lo``/``hi`` are the song's own quiet..loud signal window,
        ``dynamics`` how much it actually moves, ``mood`` a spectral+tempo shift
        of the operating point. They default to the historical fixed window with
        no per-song shaping, so live-tap / metadata frames (which carry no
        profile) behave exactly as before.
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

        # A per-song profile maps the SECTION-level envelope (so the pick follows
        # verse↔chorus↔drop, not beats); the live-tap path keeps the fast signal.
        sig = self._slow if dynamics is not None else self._signal
        target = self._resolve(sig, allowed, lo, hi, dynamics, mood)
        if target is not self._level and self._since_switch >= _PICK_DWELL_S:
            self._level = target
            self._since_switch = 0.0
        elif self._level is None:
            self._level = target  # first frame: adopt without waiting on dwell
        return self._level

    def _resolve(
        self,
        signal: float,
        allowed: tuple[SyncMode, ...],
        lo: float = _SIG_LO_REF,
        hi: float = _SIG_HI_REF,
        dynamics: float | None = None,
        mood: float = 0.0,
    ) -> SyncMode:
        """Map the signal onto a band of the enabled set, with hysteresis.

        The signal is first normalised against the loudness window ``lo..hi`` (the
        song's own quiet..loud span when a profile is supplied, else the fixed
        real-music window), then the [0, 1] range is split into one equal band per
        enabled rung. A wide dead-band on each boundary means a moment must move
        well past the edge before the rung changes, so the pick is stable.

        When a song profile is supplied, the spread is **dynamics-honest**: a song
        with real dynamics stretches its quiet↔loud across the whole enabled range
        (every selected rung gets used), while a flat, constant-loudness track is
        pulled back toward a mood-shifted centre instead of twitching between rungs.
        With no profile (``dynamics is None``) the mapping is the historical
        full-window one, unchanged.
        """
        rungs = sorted(
            (m for m in allowed if m in INTENSITY_LADDER), key=INTENSITY_LADDER.index
        ) or list(DEFAULT_AUTO_LEVELS)
        n = len(rungs)
        span = max(1e-3, hi - lo)
        norm = max(0.0, min(1.0, (signal - lo) / span))
        if dynamics is not None:
            # How much of the full range this song has earned (0 flat .. 1 dynamic).
            w = max(0.0, min(1.0, dynamics / _PICK_DYN_REF))
            mood = max(-_PICK_MOOD_MAX, min(_PICK_MOOD_MAX, mood))
            center = max(0.15, min(0.85, 0.5 + mood))
            norm = max(0.0, min(1.0, center + w * (norm - 0.5)))
        b = rungs.index(self._level) if self._level in rungs else min(n - 1, int(norm * n))
        while b < n - 1 and norm > (b + 1) / n + _PICK_HYST:
            b += 1
        while b > 0 and norm < b / n - _PICK_HYST:
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
    values, pan, lo: int, hi: int, side: float, gain: float
) -> float:
    """Mean of ``values[lo:hi]`` with each bin weighted toward this lamp's
    side of the stereo field: a bin panned to the lamp's side counts up to
    double, one panned away fades toward zero. Dividing by the bin count
    (not the weight sum) keeps a centred mix EXACTLY the unweighted mean —
    hard pans redistribute brightness across the room, never add to it.
    """
    total = 0.0
    for k in range(lo, hi):
        w = 1.0 + gain * pan[k] * side
        if w < 0.0:
            w = 0.0
        elif w > 2.0:
            w = 2.0
        total += values[k] * w
    return total / (hi - lo)


def _melbank_drive(
    frame, env: dict[str, float], info: dict, pan_gain: float = 0.0
) -> float:
    """This lamp's continuous reactive level (0..~1) from its melbank slice.

    Falls back to the lamp's coarse band envelope when the analyzer did not
    populate a melbank (e.g. unit-test frames), so the continuous layer is
    always defined and the room never goes dark purely for lack of a melbank.
    With stereo pan available, the slice is weighted toward the lamp's side
    of the stereo field so panned instruments light the matching side.
    """
    mel = getattr(frame, "melbank", None)
    if mel:
        lo, hi = info["mel_lo"], info["mel_hi"]
        if hi > lo:
            pan = getattr(frame, "pan", None)
            if pan_gain > 0.0 and pan and len(pan) >= hi:
                side = 2.0 * info["nx"] - 1.0
                return _pan_weighted_mean(mel, pan, lo, hi, side, pan_gain)
            seg = mel[lo:hi]
            return sum(seg) / len(seg)
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
            mel_drive = _melbank_drive(frame, env, info, p.pan_gain)
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
                    pop = _pan_weighted_mean(tr, pan, lo, hi, side, p.pan_gain)
                else:
                    pop = sum(tr[lo:hi]) / (hi - lo)
                bri += p.spectral_pop * pop * music
        if has_roles and role == ROLE_VOCAL:
            # The human flavour: a vocal lamp still reacts to the music (above),
            # then shimmers with the singing on top - softened a touch, but never
            # the dim, starved layer it used to be.
            bri = 0.75 * bri + p.shimmer * vocal_drive * _shimmer(t, ch.channel_id)
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
