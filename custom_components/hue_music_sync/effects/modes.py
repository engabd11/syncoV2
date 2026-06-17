"""Intensity modes as parameters for one unified renderer, with **band roles**.

Two layers of instrument reactivity work together. **Named roles** give a few
lights a dedicated job — **bass** (kick), **mid** (guitar/snare) or **vocal**
(shimmer on singing) per ``role_mix`` — and rotate musically so the show keeps
surprising. On top of that, **every** light reacts to its own slice of the full
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

The ladder — same pattern throughout, each rung harder, darker and more unified:

* **Subtle** — no dimming; one gentle spatial gradient, colour drifts + small
  per-beat steps. The calm preset.
* **Medium** — gentle club: visible dimming, soft flashes on the stronger
  beats, album colours stepping each beat across a wide spatial spread.
* **High** — the one mode that keeps the per-instrument SPATIAL split: bass
  lights snap on kicks, guitar lights pop on mid onsets, vocal lights shimmer
  with the singing; roles rotate every few bars.
* **Intense** — *unrestrained* club: the WHOLE room reacts together (no
  instrument split). Brightness follows the song's energy and every beat bursts
  all the lamps bright then falls back, colour jumping each beat. A big step up
  from High in pulse and range. Eye-safety limiter bypassed (see safety docs).
* **Extreme** — *unrestrained* maximum club: the whole room rides the energy
  from near-black in the quiet parts to full brightness in the loud ones, every
  beat detonates all the lamps like a firework and snaps back fastest (widest
  dark<->bright range, quickest reaction of the ladder), the colour jumping the
  spectrum each hit. Goes truly dark in the gaps and on breakdowns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..const import SyncMode
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


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # Seamless: NO dimming whatsoever (base == floor) — the lights hold a steady
    # bright level and only the colour moves: a slow drift plus a small step on
    # each beat, spread across the lamps as a gentle spatial gradient.
    SyncMode.SUBTLE: ModeParams(
        base=0.80, floor=0.80, bass_gain=0.0, beat_gain=0.0, beat_threshold=99.0,
        spread=0.0, colour_speed=0.04, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.008, colour_lerp=0.10, bri_attack=0.12, bri_decay=0.08,
        highlight_quantile=0.0, colour_jump=0.020, colour_spread=1.0,
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
        melbank_gain=0.45, melbank_floor=0.06, colour_flow=0.05, spectral_pop=0.35,
    ),
    # The band on your lights: bass lights snap on kicks, guitar lights pop on
    # mid onsets, and vocal lights shimmer dimly with the singing — assignments
    # rotate every 4 bars. The one mode that keeps the per-instrument SPATIAL
    # split (others go unified); colour still steps each beat, dimming visible.
    SyncMode.HIGH: ModeParams(
        base=0.07, floor=0.02, bass_gain=0.40, beat_gain=1.1, beat_threshold=1.15,
        spread=0.0, colour_speed=0.06, shimmer=0.50, colour_sat=0.8,
        colour_beat_step=0.0, colour_lerp=0.42, bri_attack=1.0, bri_decay=0.40,
        wave_gain=0.65, wave_speed=2.4, wave_width=0.30,
        anticipation_ms=80, drop_boost=0.60, build_desat=0.45,
        role_mix=(0.4, 0.3, 0.3), mid_gain=0.9, mid_threshold=1.3,
        vocal_dim=0.06, role_rotate_beats=16, hard_snap=True,
        highlight_quantile=0.40, weak_pulse=0.16, downbeat_pulse=0.45,
        colour_jump=0.07, colour_spread=0.55, full_room_accent=0.94,
        melbank_gain=0.50, melbank_floor=0.05, colour_flow=0.05, spectral_pop=0.45,
    ),
    # UNRESTRAINED (eye-safety limiter bypassed - explicit user choice, see
    # effects/safety.py). A UNIFIED room: every lamp reacts together, the whole
    # room brightening and dimming with the song's energy (energy_gain) and all
    # lamps bursting bright on each beat like fireworks (big beat_gain, no
    # instrument split). Colour jumps with the beat. A big step up from High.
    SyncMode.INTENSE: ModeParams(
        base=0.0, floor=0.0, bass_gain=0.12, beat_gain=1.9, beat_threshold=1.0,
        spread=0.0, colour_speed=0.05, shimmer=0.0, colour_sat=0.95,
        colour_beat_step=0.0, colour_lerp=0.60, energy_gain=0.25,
        bri_attack=1.0, bri_decay=0.34,
        wave_gain=0.60, wave_speed=3.0, wave_width=0.26,
        anticipation_ms=90, drop_boost=0.90, build_desat=0.55,
        role_mix=(1.0, 0.0, 0.0), hard_snap=True, flash_decay=0.74,
        highlight_quantile=0.20, weak_pulse=0.40, downbeat_pulse=0.60,
        colour_jump=0.13, colour_spread=0.12, full_room_accent=0.0,
        melbank_gain=0.15, melbank_floor=0.0, colour_flow=0.05, spectral_pop=0.0,
    ),
    # UNRESTRAINED maximum club. The whole room rides the song's energy from
    # near-black in the quiet parts to FULL brightness in the loud ones (big
    # energy_gain), and every beat detonates all the lamps at once like a
    # firework (huge beat_gain, fastest fall). Fastest, widest dark<->bright
    # range of the ladder; colour jumps hardest. One unified room, no instrument
    # split - every light reacts.
    SyncMode.EXTREME: ModeParams(
        base=0.0, floor=0.0, bass_gain=0.08, beat_gain=2.8, beat_threshold=1.0,
        spread=0.0, colour_speed=0.06, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.0, colour_lerp=0.72, energy_gain=0.32,
        bri_attack=1.0, bri_decay=0.30,
        wave_gain=0.60, wave_speed=3.6, wave_width=0.24,
        anticipation_ms=90, drop_boost=1.0, build_desat=0.60,
        role_mix=(1.0, 0.0, 0.0), hard_snap=True, flash_decay=0.64,
        accent_floor=0.15, weak_pulse=0.50, downbeat_pulse=0.70,
        highlight_quantile=0.12, colour_jump=0.20, colour_spread=0.0,
        full_room_accent=0.0,
        melbank_gain=0.0, melbank_floor=0.0, colour_flow=0.07, spectral_pop=0.0,
    ),
}

# Modes that run with the whole-field flash limiter and red guard BYPASSED.
# An explicit, documented user choice: these are club modes meant to go as hard
# as the Hue pipeline can. Subtle/Medium/High and the Movies effect stay fully
# protected. See the photosensitivity warning in the README.
UNRESTRAINED_MODES = frozenset({SyncMode.INTENSE, SyncMode.EXTREME})


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


def assign_roles(count: int, mix: tuple[float, float, float], offset: int) -> list[int]:
    """Role per light rank (left-to-right), rotated by ``offset``.

    ``mix`` gives the (bass, mid, vocal) fractions; every non-zero role gets at
    least one light when there are lights to spare, and rotation cycles the
    assignment around the room so the "band members" trade places.
    """
    if count <= 0:
        return []
    nb = int(round(mix[0] * count))
    nm = int(round(mix[1] * count))
    if mix[0] > 0.0:
        nb = max(1, nb)
    if mix[1] > 0.0 and count >= 2:
        nm = max(1, nm)
    nb = min(nb, count)
    nm = min(nm, count - nb)
    nv = count - nb - nm
    if mix[2] <= 0.0 and nv > 0:  # no vocal role wanted: hand spares to bass
        nb += nv
        nv = 0
    roles = [ROLE_BASS] * nb + [ROLE_MID] * nm + [ROLE_VOCAL] * nv
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


def _melbank_drive(frame, env: dict[str, float], info: dict) -> float:
    """This lamp's continuous reactive level (0..~1) from its melbank slice.

    Falls back to the lamp's coarse band envelope when the analyzer did not
    populate a melbank (e.g. unit-test frames), so the continuous layer is
    always defined and the room never goes dark purely for lack of a melbank.
    """
    mel = getattr(frame, "melbank", None)
    if mel:
        lo, hi = info["mel_lo"], info["mel_hi"]
        if hi > lo:
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
    weight = 0.4 + 0.6 * bass  # full on kicks, dimmer on bass-less onsets
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
        if has_roles and role == ROLE_VOCAL:
            # The quiet, human layer: a very dim light shimmering with the
            # singing / high content, deliberately capped well below the rest.
            bri = p.vocal_dim + p.shimmer * vocal_drive * _shimmer(t, ch.channel_id)
            bri = min(bri, 0.5)
        else:
            drive = mids if (has_roles and role == ROLE_MID) else bass
            bri = base_term + p.bass_gain * drive * env_mul
            # The always-alive LedFx layer: this lamp rides the exp-smoothed
            # power of its melbank slice (mapped across the room), so it keeps
            # moving with the music between beats. Beat flashes/waves are added
            # later on top of this — never gating it.
            if p.melbank_gain:
                mel_drive = _melbank_drive(frame, env, info)
                bri += (p.melbank_floor + p.melbank_gain * mel_drive) * music * env_mul
        if p.energy_gain:
            # The whole room follows the song's loudness contour together (the
            # unified "brighten on the build, dim in the breakdown" motion).
            bri += p.energy_gain * engine.energy_env
        if p.spectral_pop and tr:
            # All-instrument reactivity: pop on a fresh attack anywhere in this
            # lamp's slice of the spectrum (kick -> low lamps, snare -> low-mids,
            # guitar/lead -> mids, cymbal/air -> highs). Transient-based, so it
            # snaps bright on the hit and falls back - the club spectrum strobe.
            lo, hi = info["mel_lo"], info["mel_hi"]
            if hi > lo:
                bri += p.spectral_pop * (sum(tr[lo:hi]) / (hi - lo)) * music
        if p.spread:
            bri += p.spread * env.get(info["band"], 0.0)
        if p.height_freq:
            # Lamps high in the room favour treble, low lamps favour bass.
            bri += p.height_freq * env.get(info["hband"], 0.0)
        if p.depth_wash:
            # Back/far lamps carry a gentle ambient wash; front lamps stay reactive.
            bri += p.depth_wash * (1.0 - info["ny"])
        if p.wave_gain and waves:
            # Beat wavefront(s) sweeping out from the room's low centre. Vocal
            # lights only catch a fraction, keeping their quiet identity.
            d = info["dist_origin"]
            amp = 0.0
            for w in waves:
                amp += w.amplitude_at(d)
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
