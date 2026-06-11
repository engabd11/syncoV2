"""Intensity modes as parameters for one unified renderer, with **band roles**.

The signature trick is the per-bulb *instrument split*: each light is assigned a
role — **bass** (kick), **mid** (guitar/snare) or **vocal** (shimmer on singing
and high content) — per the mode's ``role_mix``, and the assignments rotate
musically (every few bars, and on drops) so the show keeps surprising. A light
"is" the bass: it rides the bass envelope and snaps on kicks; the next light
"is" the guitar and pops on mid onsets; a third shimmers dimly with the vocal.

The ladder:

* **Subtle** — no dimming at all; colour shifting only.
* **Medium** — the classic club look (visible dimming, wavefront per beat).
* **High** — Intense plus a vocal role: bass + guitar lights at full aggression
  with shimmering dim lights carrying the singing.
* **Intense** — *unrestrained*: bass and guitar roles only, hard snaps, the
  eye-safety limiter is bypassed (explicit user choice, see safety docs).
* **Extreme** — *unrestrained*: every light is bass, only the big kicks count,
  and everything moves fast in a dark room.
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


MODE_PARAMS: dict[SyncMode, ModeParams] = {
    # Seamless: NO dimming whatsoever (base == floor) — the lights hold a steady
    # bright level and only the colour moves: a continuous drift plus a step on
    # each beat so the hues still groove with the song. The calmest preset.
    SyncMode.SUBTLE: ModeParams(
        base=0.80, floor=0.80, bass_gain=0.0, beat_gain=0.0, beat_threshold=99.0,
        spread=0.0, colour_speed=0.04, shimmer=0.0, colour_sat=1.0,
        colour_beat_step=0.008, colour_lerp=0.08, bri_attack=0.12, bri_decay=0.08,
    ),
    # The classic club look (what Intense used to be): dim baseline with visible
    # dimming between beats, a strong wavefront per kick, soft desaturated album
    # colours stepping on the beat. Every light rides the bass together.
    SyncMode.MEDIUM: ModeParams(
        base=0.12, floor=0.06, bass_gain=0.16, beat_gain=0.9, beat_threshold=1.4,
        spread=0.10, colour_speed=0.05, shimmer=0.15, colour_sat=0.6,
        colour_beat_step=0.030, colour_lerp=0.30,
        wave_gain=0.85, wave_speed=2.2, wave_width=0.30, height_freq=0.45,
        depth_wash=0.10, anticipation_ms=80, drop_boost=0.50, build_desat=0.50,
    ),
    # The band on your lights: bass lights snap on kicks, guitar lights pop on
    # mid onsets, and vocal lights shimmer dimly with the singing — assignments
    # rotate every 4 bars. Full Intense aggression underneath, but the vocal
    # role keeps a quiet, human layer in the room.
    SyncMode.HIGH: ModeParams(
        base=0.10, floor=0.04, bass_gain=0.55, beat_gain=1.0, beat_threshold=1.15,
        spread=0.0, colour_speed=0.06, shimmer=0.55, colour_sat=0.75,
        colour_beat_step=0.030, colour_lerp=0.35, bri_attack=1.0, bri_decay=0.42,
        wave_gain=0.70, wave_speed=2.4, wave_width=0.30,
        anticipation_ms=80, drop_boost=0.60, build_desat=0.45,
        role_mix=(0.4, 0.3, 0.3), mid_gain=0.85, mid_threshold=1.3,
        vocal_dim=0.07, role_rotate_beats=16, hard_snap=True,
    ),
    # UNRESTRAINED (the eye-safety limiter is bypassed for this mode — explicit
    # user choice, see effects/safety.py): bass and guitar split the room 2:1,
    # both snapping hard to full; kicks land at a low threshold and mid onsets
    # (guitar/snare) hit their own lights even off the grid. Roles rotate every
    # 4 bars so the energy keeps moving around the room.
    SyncMode.INTENSE: ModeParams(
        base=0.06, floor=0.02, bass_gain=0.50, beat_gain=1.2, beat_threshold=1.15,
        spread=0.0, colour_speed=0.07, shimmer=0.0, colour_sat=0.9,
        colour_beat_step=0.045, colour_lerp=0.45, bri_attack=1.0, bri_decay=0.50,
        wave_gain=1.1, wave_speed=2.8, wave_width=0.28,
        anticipation_ms=90, drop_boost=0.80, build_desat=0.55,
        role_mix=(0.67, 0.33, 0.0), mid_gain=1.0, mid_threshold=1.25,
        role_rotate_beats=16, hard_snap=True,
    ),
    # UNRESTRAINED maximum: a dark room where every light is bass and only the
    # BIG kicks count (high threshold) — each one slams every lamp toward full
    # and launches a fast wavefront, with hard fast colour jumps. Roles don't
    # split (the kick owns the room); rotation still reseeds the colour layout
    # every 2 bars so it never settles.
    SyncMode.EXTREME: ModeParams(
        base=0.0, floor=0.0, bass_gain=0.10, beat_gain=1.5, beat_threshold=1.7,
        spread=0.0, colour_speed=0.15, shimmer=0.30, colour_sat=0.92,
        colour_beat_step=0.085, colour_lerp=0.60, bri_attack=1.0, bri_decay=0.55,
        wave_gain=1.6, wave_speed=3.6, wave_width=0.22,
        anticipation_ms=90, drop_boost=1.0, build_desat=0.60,
        role_mix=(1.0, 0.0, 0.0), role_rotate_beats=8, hard_snap=True,
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


def kick_flash(params: ModeParams, strength: float, bass: float) -> float:
    """Flash a bass-role light gets from a qualifying kick (0 if it doesn't).

    Weighted by bass content so the snaps track the kick rather than incidental
    onsets. The engine keeps this as a fast-decaying per-light overlay so beats
    snap to full independent of the slower continuous smoothing.
    """
    if strength >= params.beat_threshold:
        weight = 0.4 + 0.6 * bass  # full on kicks, dimmer on bass-less onsets
        return params.beat_gain * min(1.0, strength / 2.0) * weight
    return 0.0


def mid_flash(params: ModeParams, strength: float) -> float:
    """Flash a mid-role light gets from a guitar/snare (non-bass) onset."""
    if params.mid_gain > 0.0 and strength >= params.mid_threshold:
        return params.mid_gain * min(1.0, strength / 2.0)
    return 0.0


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
    base_mul = 0.75 + 0.25 * lvl
    env_mul = 0.7 + 0.3 * lvl
    wave_mul = 0.6 + 0.4 * lvl
    span = 0.4 + 0.6 * lvl
    base_term = p.floor + (p.base - p.floor) * base_mul
    # Rotating modes also reseed the colour layout on each rotation (an
    # irrational-ish step so the arrangement never repeats), so the room's
    # colour geography moves with the band.
    rot = 0.37 * engine.role_offset if p.role_rotate_beats > 0 else 0.0

    waves = engine.active_waves
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
        if p.energy_gain:
            bri += p.energy_gain * frame.energy  # follow overall loudness (movie)
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
        # per-beat steps), so colour moves to the beat.
        colour = engine.palette.sample(info["xrank"] * span + rot + engine.colour_phase)
        # Theme-faithful value: a dark palette swatch (dark silver, deep purple)
        # renders as dimmer light, so moody album art gives a moody show. The
        # engine's chroma pipeline renormalises colour, so the value must be
        # folded into brightness here. Full-value palettes are unaffected.
        cval = max(colour)
        bri *= 0.35 + 0.65 * cval
        out[ch.channel_id] = (colour, bri)

    return out
