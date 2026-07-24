"""Fireworks effect: spatial bursts ignite from the music and expand outward.

On a kick (bass) or a snare/guitar (mid) onset a burst is *launched* from a point
in the room — bass bursts low/left, mid bursts higher/right — and expands outward
as a bright shell of the palette colour for that region before fading, like a
firework shell blooming and trailing off. Between bursts a soft palette glow
breathes with the music so the room is never black.

Fireworks has its OWN onset sensitivity, independent of the intensity ladder's
``beat_threshold`` (which on the calm rungs is deliberately near-unreachable), so
it stays lively on Subtle and slams on Extreme. The chosen intensity rung tunes
the *feel* — burst size, fade speed and how much resting glow sits underneath —
via :data:`_RUNG_FEEL`.

Unlike the music renderer this owns its own per-burst state and decay, so it is
deliberately *not* run through the engine's brightness smoothing — the snap-and-
expand is the whole point.
"""

from __future__ import annotations

import math
import random

from ..color.palette import RGB
from ..const import SyncMode
from .modes import event_gates

# Shell shape / life.
_EXPAND = 0.9          # shell expansion speed (xrank units per second)
_SHELL_WIDTH = 0.34    # thickness of the expanding ring (xrank units)
_MAX_BURSTS = 10       # cap concurrent bursts (cheap; avoids a colour wash-out)
_BURST_LIFE = 2.0      # cull a burst after this long (fully faded by then)
_HEIGHT_WEIGHT = 0.7   # how much lamp height (nz) counts vs left-right in the shell

# Fireworks' OWN onset sensitivity — NOT the intensity mode's beat_threshold (99
# on Subtle), so bursts fire on every rung. Bass launches on kicks, mid on
# snare/guitar onsets. Values are on the frame's gated onset strength (0..~3).
_BASS_THRESH = 0.55
_MID_THRESH = 0.75

# Auto-launch a gentle burst if this long passes with no qualifying onset, so
# quiet passages still sparkle instead of going dark.
_AUTO_LAUNCH_S = 1.6

# Per-rung feel: (burst_gain, glow, tau_seconds). Gentler rungs -> smaller bursts
# over a higher resting glow with a longer, softer trail; punchy rungs -> bigger,
# brighter bursts over a darker base with a snappier trail. Auto/unknown uses the
# middle of the ladder.
_RUNG_FEEL = {
    SyncMode.SUBTLE:  (0.95, 0.22, 0.72),
    SyncMode.MEDIUM:  (1.05, 0.14, 0.60),
    SyncMode.HIGH:    (1.15, 0.10, 0.52),
    SyncMode.INTENSE: (1.30, 0.07, 0.46),
    SyncMode.EXTREME: (1.45, 0.05, 0.40),
}
_FEEL_DEFAULT = (1.10, 0.12, 0.55)

# Palette spread used to colour bursts by their epicentre, so a burst takes the
# same palette colour those room lamps use in the music renderer (coherent, not
# random). Bass epicentres (low xrank) and mid epicentres (high xrank) therefore
# land on different palette colours automatically.
_COLOUR_SPREAD = 0.7


class _Burst:
    """One live firework: an epicentre, a colour, a strength and an age."""

    __slots__ = ("ex", "ez", "color", "strength", "age")

    def __init__(self, ex: float, ez: float, color: RGB, strength: float) -> None:
        self.ex = ex            # epicentre xrank (0..1, left->right / low->high)
        self.ez = ez            # epicentre height (nz, 0..1)
        self.color = color      # normalised RGB (max channel == 1)
        self.strength = strength
        self.age = 0.0


class FireworksEffect:
    """Stateful spatial burst renderer driven by detected kick/snare onsets."""

    def __init__(self, seed: int | None = None) -> None:
        self._bursts: list[_Burst] = []
        self._since_launch = 0.0
        self._rng = random.Random(seed)

    def reset(self) -> None:
        self._bursts.clear()
        self._since_launch = 0.0

    def _feel(self, engine):
        return _RUNG_FEEL.get(getattr(engine, "mode", None), _FEEL_DEFAULT)

    def render(self, engine, frame, dt: float) -> dict[int, RGB]:
        """Return final per-channel RGB (already master-brightness scaled)."""
        params = engine.params
        channels = engine.channels
        if not channels:
            return {}
        burst_gain, glow_feel, tau = self._feel(engine)

        # Age and cull existing bursts.
        for b in self._bursts:
            b.age += dt
        if self._bursts:
            self._bursts = [b for b in self._bursts if b.age < _BURST_LIFE]

        # --- Decide whether to ignite new bursts this frame. Fireworks uses its
        # own thresholds; the mode's event gates still apply so narrowband
        # (vocal/tonal) onsets are muted and burst size follows absolute loudness
        # (quiet pluck small, drop slams). Kick and snare fire independent bursts
        # in their own region of the room. ---
        self._since_launch += dt
        amp_scale, width_gate = event_gates(params, frame.salience, frame.onset_width)
        launched = False
        bass_gated = frame.bass_strength * width_gate
        if frame.bass_beat and bass_gated >= _BASS_THRESH:
            self._launch(engine, "bass", bass_gated * amp_scale)
            launched = True
        mid_gated = frame.mid_strength * width_gate
        if frame.mid_beat and mid_gated >= _MID_THRESH:
            self._launch(engine, "mid", mid_gated * amp_scale)
            launched = True
        if launched:
            self._since_launch = 0.0
        elif self._since_launch >= _AUTO_LAUNCH_S and engine.energy_env > 0.02:
            # Keep quiet passages sparkling, but never burst into a genuinely
            # silent stream (a finished/muted track just fades to the glow floor).
            self._since_launch = 0.0
            band = "bass" if self._rng.random() < 0.5 else "mid"
            self._launch(engine, band, 0.8)

        # --- Compose: a soft palette afterglow that breathes with energy, plus
        # each lamp's strongest expanding-shell contribution (keep that burst's
        # colour so bursts stay crisp instead of muddying together). ---
        mb = engine.brightness
        glow_level = glow_feel * (0.4 + 0.6 * engine.energy_env)
        out: dict[int, RGB] = {}
        for ch in channels:
            cid = ch.channel_id
            info = engine.cmap[cid]
            x = info["xrank"]
            z = info["nz"]
            # Afterglow floor: this lamp's palette colour, gently lit.
            gc = engine.palette.sample(x * _COLOUR_SPREAD + engine.colour_phase)
            gm = max(gc) or 1.0
            r = gc[0] / gm * glow_level
            g = gc[1] / gm * glow_level
            bl = gc[2] / gm * glow_level
            # Strongest live shell reaching this lamp.
            best = 0.0
            bcol: RGB | None = None
            for burst in self._bursts:
                d = math.hypot(x - burst.ex, _HEIGHT_WEIGHT * (z - burst.ez))
                radius = _EXPAND * burst.age
                env = math.exp(-((d - radius) / _SHELL_WIDTH) ** 2)
                fade = math.exp(-burst.age / tau)
                contrib = burst.strength * env * fade
                if contrib > best:
                    best = contrib
                    bcol = burst.color
            if bcol is not None and best > 0.0:
                bb = min(1.0, best * burst_gain)
                r = max(r, bcol[0] * bb)
                g = max(g, bcol[1] * bb)
                bl = max(bl, bcol[2] * bb)
            out[cid] = (min(1.0, r) * mb, min(1.0, g) * mb, min(1.0, bl) * mb)
        return out

    def _launch(self, engine, band: str, strength: float) -> None:
        if len(self._bursts) >= _MAX_BURSTS:
            self._bursts.pop(0)  # drop the oldest so fresh bursts still land
        channels = engine.channels
        cmap = engine.cmap
        # Ignite at a real lamp in this band's region — bass on the low/left lamps,
        # mid on the higher/right lamps — so the burst always lands on a light
        # (bright at t=0) and then expands to its neighbours. Colour it by that
        # lamp's palette position, so it matches the colour that lamp carries in
        # the music renderer (coherent, not random).
        if band == "bass":
            cands = [c for c in channels if cmap[c.channel_id]["xrank"] <= 0.5]
        else:
            cands = [c for c in channels if cmap[c.channel_id]["xrank"] >= 0.5]
        if not cands:
            cands = channels
        ch = self._rng.choice(cands)
        info = cmap[ch.channel_id]
        ex = info["xrank"]
        ez = info["nz"]
        col = engine.palette.sample(ex * _COLOUR_SPREAD + engine.colour_phase)
        m = max(col)
        col = (1.0, 1.0, 1.0) if m <= 1e-6 else (col[0] / m, col[1] / m, col[2] / m)
        s = max(0.35, min(1.4, strength))
        self._bursts.append(_Burst(ex, ez, col, s))
