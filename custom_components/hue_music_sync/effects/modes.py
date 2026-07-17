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
* **Intense** — *unrestrained*: the same fast dim<->bright SWING as Extreme —
  the room brightens on the beat over a few frames (a slew-limited swell, not a
  1-frame strobe) and the colour shifts each hit — but with a HIGHER dark FLOOR
  so it keeps a soft glow in the gaps instead of going black. The floor is the
  deliberate, only-real difference from Extreme: gentler, more comfortable.
  Eye-safety limiter bypassed (see safety docs).
* **Extreme** — *unrestrained* maximum club: the same quick swing, but a TRUE
  dark room (floor 0) — the quiet parts go black and every beat brightens the
  whole room out of the dark, colour jumping the spectrum each hit. Widest
  dark<->bright range of the ladder; still a smooth swell, never a strobe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..const import AUTO_BPM_HIGH, AUTO_BPM_LOW, AUTO_BPM_MARGIN, SyncMode
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
    # --- perceptual brightness curve -------------------------------------------
    # Exponent applied to the music-driven brightness headroom above the floor
    # at emit time. Perceived light follows ~cube-root of luminance while
    # perceived loudness follows ~p^0.6 of amplitude, so a LINEAR mapping
    # under-responds: the chorus never LOOKS much louder than the verse.
    # >1 expands the visual dynamic range toward perceptual proportionality
    # (endpoints preserved: full stays full, the floor stays the floor).
    # 1.0 = the legacy linear mapping (Subtle's base==floor makes it inert
    # there anyway; Movies stays linear on purpose — calm over contrast).
    bri_gamma: float = 1.0
    # --- melody follow (what to react to when there is no beat) ---------------
    # In passages with no discernible beat (intros, bridges, ballads — rhythm
    # confidence low) the room follows the LEAD line instead of falling to a
    # flat wash: lead-note onsets (frame.note_beat — broadband-stream events
    # that are not bass-dominant) drive a soft swell on the vocal-role lamps
    # (all lamps when role-less). Crossfades out as rhythm confidence returns,
    # so the verse→drop handover is seamless. 0 disables.
    melody_gain: float = 0.0
    # --- treble micro-motion (hi-hat ticks) ------------------------------------
    # Tiny texture pops on the vocal-role / high lamps from the dedicated
    # hi-hat/shaker onset stream (frame.high_beat), scaled by rhythm
    # confidence so they only appear once a groove is established. Amplitude
    # is deliberately small (≤ ~0.1): texture, never flashing. 0 disables.
    tick_gain: float = 0.0


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
        bri_gamma=1.35, melody_gain=0.30, tick_gain=0.05,
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
        bri_gamma=1.45, melody_gain=0.40, tick_gain=0.10,
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
        bri_gamma=1.5, melody_gain=0.35, tick_gain=0.08,
    ),
    # UNRESTRAINED maximum club. The same quick dim<->bright SWING as Intense,
    # but a TRUE dark room: floor 0, so the quiet parts go black and every beat
    # brightens the whole room out of the dark (a fast smoothed swell, no longer
    # a 1-frame strobe), colour jumping the spectrum each hit. Rides the energy
    # widest of the ladder; one unified room, no instrument split.
    SyncMode.EXTREME: ModeParams(
        base=0.0, floor=0.0, bass_gain=0.06, beat_gain=1.8, beat_threshold=1.0,
        spread=0.0, colour_speed=0.06, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.0, colour_lerp=0.65, energy_gain=0.12,
        bri_attack=1.0, bri_decay=0.42,
        bri_slew=0.24, flash_decay=0.80,
        wave_gain=0.50, wave_speed=3.4, wave_width=0.24,
        anticipation_ms=90, drop_boost=1.0, build_desat=0.60,
        role_mix=(1.0, 0.0, 0.0), hard_snap=True,
        accent_floor=0.15, weak_pulse=0.42, downbeat_pulse=0.65,
        highlight_quantile=0.16, colour_jump=0.20, colour_spread=0.0,
        full_room_accent=0.0,
        melbank_gain=0.14, melbank_floor=0.0, colour_flow=0.03, spectral_pop=0.45,
        salience_gamma=0.6, width_min=0.05, nobeat_flash=0.20,
        predrop_depth=0.85, phrase_bars=4, phrase_colour_shift=0.08,
        pan_gain=0.4,
        # The widest dark<->bright swing earns the strongest perceptual curve;
        # no ticks — the true dark room stays clean between hits.
        bri_gamma=1.6, melody_gain=0.25, tick_gain=0.0,
    ),
}

# Modes that run the RELAXED flash limiter (a high budget that real music never
# hits — see safety.RELAXED_MAX_FLASHES_PER_S) instead of the strict WCAG one.
# An explicit, documented user choice: these are club modes meant to go as hard
# as the Hue pipeline can on musical content, but pathological strobe output is
# still hard-capped — there is no fully-unlimited path. Subtle/Medium/High and
# the Movies effect always get the strict limiter. See the photosensitivity
# warning in the README.
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


def auto_mode_for_track(
    bpm: float, onset_rate: float, energy_mean: float
) -> SyncMode | None:
    """Resolve Auto intensity from a track map's MUSICAL profile, not BPM alone.

    A 120 BPM ballad and 120 BPM techno should not get the same level: the
    score blends tempo with percussiveness (broadband onsets per second — how
    much the track actually *hits*) and its mean loudness. Decided once per
    track (the map is authoritative), so no hysteresis is needed; returns None
    when the profile looks unmeasured (pre-v5 map) so the caller falls back to
    the BPM ladder. Never returns Intense/Extreme — those stay manual-only.
    """
    if bpm <= 0 or onset_rate <= 0.0:
        return None
    tempo = min(1.0, max(0.0, (bpm - 85.0) / 50.0))       # 85..135 BPM -> 0..1
    perc = min(1.0, onset_rate / 2.5)                      # ~2.5 hits/s = full
    energy = min(1.0, max(0.0, energy_mean / 0.6))         # p95-normalised mean
    score = 0.45 * tempo + 0.35 * perc + 0.20 * energy
    if score < 0.35:
        return SyncMode.SUBTLE
    if score > 0.62:
        return SyncMode.HIGH
    return SyncMode.MEDIUM


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
    # Chorus recall: the track's recurring highest-energy section opens the
    # full colour span and rolls slightly bigger waves — the same identity on
    # every occurrence, so the chorus visibly IS the chorus each time.
    if getattr(engine, "chorus_now", False):
        span = min(1.0, span * 1.25)
        wave_mul = min(1.0, wave_mul * 1.15)
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
        if p.melody_gain:
            # Melody follow: in beatless passages (the engine crossfades this in
            # as rhythm confidence falls) the lead line's note onsets swell the
            # room — vocal-role lamps carry it fully, the rest at half, so the
            # song's story never drops to a flat wash between grooves.
            ml = getattr(engine, "melody_level", 0.0)
            if ml > 0.0:
                w = 1.0 if (not has_roles or role == ROLE_VOCAL) else 0.5
                bri += p.melody_gain * ml * w
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
        colour = engine.palette.sample(
            cpos + rot + engine.colour_phase + getattr(engine, "section_offset", 0.0)
        )
        # Theme-faithful value: a dark palette swatch (dark silver, deep purple)
        # renders as dimmer light, so moody album art gives a moody show. The
        # engine's chroma pipeline renormalises colour, so the value must be
        # folded into brightness here. Full-value palettes are unaffected.
        cval = max(colour)
        bri *= 0.35 + 0.65 * cval
        out[ch.channel_id] = (colour, bri)

    return out
