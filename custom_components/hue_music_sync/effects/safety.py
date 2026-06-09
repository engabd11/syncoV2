"""Final-stage eye-safety limiter — the last thing every frame passes through.

Audio-reactive lighting that fills a large share of the visual field can, on
aggressive content, produce rapid whole-room brightness swings. The relevant
guidance (WCAG 2.3.1 "Three Flashes or Below Threshold", derived from broadcast
photosensitive-epilepsy limits) caps a flashing field at **3 flashes/second**,
where a "flash" is a pair of opposing luminance changes of ≥10% of max, and
treats **saturated red** more strictly still.

:class:`FieldSafety` enforces those limits as a *non-bypassable* final stage:
every renderer (Music, Movies, Fireworks, idle glow) and every intensity is fed
through it before encoding, so no effect or setting can defeat it. It is
deliberately transparent on well-behaved content — it only engages when the
aggregate field actually starts to strobe — and degrades gracefully by
compressing the *global* brightness swing while preserving each light's colour
and the spatial pattern between lights (which is what makes the show look good).

Pure and dt-driven (no Home Assistant or hardware dependency) so the invariants
are unit-tested directly.
"""

from __future__ import annotations

from collections import deque

from ..color.palette import RGB

# A "flash" half-transition must move the field luminance by at least this much
# (fraction of full scale) to count, per the WCAG 10%-of-max threshold.
FLASH_DELTA = 0.10
# Hard ceiling on full-field flashes within any rolling one-second window.
MAX_FLASHES_PER_S = 3
# Saturated red has a separate, stricter threshold; allow far fewer red flashes
# before the guard starts pulling the colour toward white.
MAX_RED_FLASHES_PER_S = 1
RED_SATURATION = 0.55  # field is "red" when red dominance exceeds this

_WINDOW_S = 1.0
_EMA_ALPHA = 0.04  # slow anchor (~0.5 s) the field is pinned toward when limiting
_ENGAGE_RATE = 1.0  # compression engages instantly once the budget is exceeded
_RELEASE_RATE = 0.02  # …and releases slowly, so it can't oscillate back to strobing
_DIR_EPS = 1e-4


def _field_brightness(colors: dict[int, RGB]) -> float:
    """Perceived whole-field brightness: mean of each light's max channel."""
    if not colors:
        return 0.0
    return sum(max(c) for c in colors.values()) / len(colors)


def _field_redness(colors: dict[int, RGB]) -> float:
    """0..1 red dominance of the lit field (red far above green/blue)."""
    if not colors:
        return 0.0
    total = 0.0
    lit = 0.0
    for r, g, b in colors.values():
        m = max(r, g, b)
        if m <= 1e-4:
            continue
        lit += m
        total += m * max(0.0, (r - max(g, b)) / (r + 1e-6))
    return total / lit if lit > 1e-6 else 0.0


class FieldSafety:
    """Stateful whole-field flash limiter + saturated-red guard.

    One instance per streaming session (it holds the running field state).
    """

    def __init__(
        self,
        max_flashes_per_s: int = MAX_FLASHES_PER_S,
        flash_delta: float = FLASH_DELTA,
    ) -> None:
        self._max_flashes = max_flashes_per_s
        self._delta = flash_delta
        self._t = 0.0
        self._ema: float | None = None
        self._prev_field: float | None = None
        self._last_extreme = 0.0
        self._dir = 0  # +1 rising, -1 falling, 0 unknown
        self._half = 0  # half-transitions; two make one flash
        self._flashes: deque[float] = deque()  # timestamps within the window
        self._comp = 1.0  # 1 = transparent, 0 = field pinned to its slow average

    def reset(self) -> None:
        self._ema = None
        self._prev_field = None
        self._dir = 0
        self._half = 0
        self._flashes.clear()
        self._comp = 1.0

    def process(self, colors: dict[int, RGB], dt: float) -> dict[int, RGB]:
        """Return a copy of ``colors`` with whole-field flashing bounded.

        ``dt`` is the wall-clock time since the previous emitted frame.
        """
        if not colors:
            return colors
        self._t += dt
        field = _field_brightness(colors)
        if self._ema is None:
            self._ema = field
            self._prev_field = field
            self._last_extreme = field

        # Decide compression from the flash budget *measured on what we emitted*
        # last frame, then ramp the compression factor (fast in, slow out).
        self._prune()
        # Engage one flash *before* the ceiling: when compression kicks in there
        # is always one half-formed flash already in flight, so triggering at the
        # ceiling itself would let a final (N+1)th flash leak out. Triggering at
        # ceiling-1 keeps the emitted field at or below ``max_flashes`` per second.
        over = len(self._flashes) >= max(1, self._max_flashes - 1)
        comp_target = 0.0 if over else 1.0
        rate = _ENGAGE_RATE if comp_target < self._comp else _RELEASE_RATE
        self._comp += (comp_target - self._comp) * rate
        limiting = self._comp < 0.999

        # The anchor only tracks the field while we are *not* limiting; freezing
        # it during a strobe keeps it from drifting into the swing (which would
        # let the compressed field leak small flashes back through).
        if not limiting:
            self._ema += (field - self._ema) * _EMA_ALPHA

        # While limiting, pull the field's *temporal swing* toward that anchor two
        # ways at once: a gain compresses the bright peaks, and a white floor
        # lifts the dark dips so a black↔white strobe can't survive (a pure
        # multiplicative gain can never lift a true-black frame). Colour and
        # per-light ratios are otherwise untouched, and both collapse to a no-op
        # when ``comp == 1`` (not limiting), so normal content is unchanged.
        target = field if not limiting else self._ema + (field - self._ema) * self._comp
        gain = max(0.0, min(2.0, target / field)) if field > 1e-6 else 1.0
        floor = max(0.0, target - 0.5 * self._delta) if limiting else 0.0

        out: dict[int, RGB] = {}
        for cid, (r, g, b) in colors.items():
            r, g, b = r * gain, g * gain, b * gain
            lift = floor - max(r, g, b)
            if lift > 0.0:  # raise the dark floor with neutral white
                r, g, b = r + lift, g + lift, b + lift
            out[cid] = (min(1.0, r), min(1.0, g), min(1.0, b))

        emitted = _field_brightness(out)
        self._track_flash(emitted)
        out = self._apply_red_guard(out)
        return out

    def _prune(self) -> None:
        cutoff = self._t - _WINDOW_S
        while self._flashes and self._flashes[0] < cutoff:
            self._flashes.popleft()

    def _track_flash(self, field: float) -> None:
        """Detect opposing-transition pairs in the emitted field brightness."""
        prev = self._prev_field
        if prev is None:
            self._prev_field = field
            return
        if field > prev + _DIR_EPS:
            cur = 1
        elif field < prev - _DIR_EPS:
            cur = -1
        else:
            cur = self._dir
        if self._dir != 0 and cur != 0 and cur != self._dir:
            # Direction reversed: ``prev`` was a local extreme.
            if abs(prev - self._last_extreme) >= self._delta:
                self._half += 1
                if self._half % 2 == 0:  # two half-transitions == one full flash
                    self._flashes.append(self._t)
            self._last_extreme = prev
        if cur != 0:
            self._dir = cur
        self._prev_field = field

    def _apply_red_guard(self, colors: dict[int, RGB]) -> dict[int, RGB]:
        """Desaturate a flashing saturated-red field toward white.

        Saturated red carries a stricter flash threshold, so even a couple of
        red flashes per second pull the colour toward white (which both lowers
        the red-flash risk and softens the look) without dimming the field.
        """
        if len(self._flashes) <= MAX_RED_FLASHES_PER_S:
            return colors
        redness = _field_redness(colors)
        if redness < RED_SATURATION:
            return colors
        # Scale desat with how far over the red budget we are (cap at 50% white).
        over = len(self._flashes) - MAX_RED_FLASHES_PER_S
        mix = min(0.5, 0.15 * over) * redness
        if mix <= 0.0:
            return colors
        out: dict[int, RGB] = {}
        for cid, (r, g, b) in colors.items():
            m = max(r, g, b)
            white = m * mix  # add white at the light's own level, don't brighten
            out[cid] = (
                min(m, r * (1.0 - mix) + white),
                min(m, g * (1.0 - mix) + white),
                min(m, b * (1.0 - mix) + white),
            )
        return out
