"""Final-stage eye-safety limiter — the last thing every frame passes through.

Audio-reactive lighting that fills a large share of the visual field can, on
aggressive content, produce rapid whole-room brightness swings. The relevant
guidance (WCAG 2.3.1 "Three Flashes or Below Threshold", derived from broadcast
photosensitive-epilepsy limits) caps a flashing field at **3 flashes/second**,
where a "flash" is a pair of opposing luminance changes of ≥10% of max, and
treats **saturated red** more strictly still.

:class:`FieldSafety` enforces those limits as the final stage before encoding
for every renderer (Music, Movies, Fireworks, idle glow). **Intense** runs a
*relaxed* limiter instead of the WCAG one: a much higher flash budget
(:data:`RELAXED_MAX_FLASHES_PER_S`) that never engages on real music but still
hard-caps true strobe output. **Extreme** goes further — at the user's explicit
request it BYPASSES the limiter entirely (``coordinator._bypass_limiter``) so
its sharp, fast flashing is untouched; it is the one fully-unlimited path.
Both remain unsuitable for photosensitive viewers (see the README's warning);
Subtle, Medium, High and the Movies effect always get the strict limiter.
When engaged it is deliberately transparent on well-behaved content —
it only acts when the aggregate field actually starts to strobe — and degrades
gracefully by compressing the *global* brightness swing while preserving each
light's colour and the spatial pattern between lights (which is what makes the
show look good).

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
# The club modes (Intense/Extreme) run a RELAXED limiter instead of none at
# all: this budget is far above any musical beat rate (8 flashes/s ≈ a beat
# flash at 480 BPM), so it is transparent for the hardest-hitting real music
# — the club character is untouched — while still hard-capping pathological
# strobe content (broken analysis, adversarial input, a stuck oscillation).
# It is NOT a WCAG-compliant level; those modes remain unsuitable for
# photosensitive viewers and the README says so.
RELAXED_MAX_FLASHES_PER_S = 8
# Saturated red has a separate, stricter threshold; allow far fewer red flashes
# before the guard starts pulling the colour toward white.
MAX_RED_FLASHES_PER_S = 1
RED_SATURATION = 0.55  # field is "red" when red dominance exceeds this

_WINDOW_S = 1.0
_EMA_ALPHA = 0.04  # slow anchor (~0.5 s) the field is pinned toward when limiting
_ENGAGE_RATE = 1.0  # compression engages instantly once the budget is exceeded
_RELEASE_RATE = 0.02  # …and releases slowly, so it can't oscillate back to strobing
# Release is also gated on the *incoming* field going quiet: while the content is
# still actively swinging we keep the field pinned, so a sustained strobe can't
# leak a few flashes back through on every slow-release cycle.
_ACTIVITY_DECAY = 0.92  # peak-hold decay of the raw-field activity measure
_ACTIVITY_CALM = 0.04  # per-frame field swing below which the content is "calm"
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
        *,
        calm_gated: bool = True,
    ) -> None:
        # ``calm_gated`` (the strict default) also ENGAGES on hard-swinging
        # content before the budget is spent and refuses to release until the
        # content calms — the conservative WCAG posture. The relaxed club-mode
        # limiter sets it False so engagement is keyed purely to the flash
        # budget: musical full-range swings pass untouched, and only genuinely
        # over-budget strobing pins the field.
        self._max_flashes = max_flashes_per_s
        self._delta = flash_delta
        self._calm_gated = calm_gated
        self._t = 0.0
        self._ema: float | None = None
        self._prev_field: float | None = None
        self._last_extreme = 0.0
        self._dir = 0  # +1 rising, -1 falling, 0 unknown
        self._half = 0  # half-transitions; two make one flash
        self._flashes: deque[float] = deque()  # timestamps within the window
        self._comp = 1.0  # 1 = transparent, 0 = field pinned to its slow average
        self._prev_raw: float | None = None
        self._activity = 0.0  # how hard the incoming field is currently swinging

    def reset(self) -> None:
        self._ema = None
        self._prev_field = None
        self._dir = 0
        self._half = 0
        self._flashes.clear()
        self._comp = 1.0
        self._prev_raw = None
        self._activity = 0.0

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

        # Track how hard the *incoming* field is swinging (peak-held), so we only
        # release once the content genuinely calms — not merely on a timer.
        if self._prev_raw is None:
            self._prev_raw = field
        self._activity = max(self._activity * _ACTIVITY_DECAY, abs(field - self._prev_raw))
        self._prev_raw = field

        # Decide compression from the flash budget *measured on what we emitted*
        # last frame, then ramp the compression factor (fast in, slow out).
        self._prune()
        # Engage one flash *before* the ceiling: when compression kicks in there
        # is always one half-formed flash already in flight, so triggering at the
        # ceiling itself would let a final (N+1)th flash leak out. Triggering at
        # ceiling-1 keeps the emitted field at or below ``max_flashes`` per second.
        over = len(self._flashes) >= max(1, self._max_flashes - 1)
        # Only release when both the flash budget is clear *and* the incoming
        # field has gone quiet; otherwise stay pinned through a sustained strobe.
        # (Budget-only when not calm-gated: the relaxed club-mode limiter must
        # not engage on musical swings that are still within its budget.)
        calm = self._activity < _ACTIVITY_CALM
        if self._calm_gated:
            comp_target = 1.0 if (not over and calm) else 0.0
        else:
            comp_target = 0.0 if over else 1.0
        rate = _ENGAGE_RATE if comp_target < self._comp else _RELEASE_RATE
        self._comp += (comp_target - self._comp) * rate
        limiting = self._comp < 0.999

        # The anchor tracks the field while we are *not* limiting. While limiting
        # it may still RISE toward the field but never FALL: a stale-dark anchor
        # (e.g. seeded on the dark frame right after a mode switch into a club
        # mode, then frozen while the new mode flashes) would otherwise pin the
        # room black forever — the user had to cycle through Subtle to recover.
        # Letting it climb can only *reduce* compression, never create a flash,
        # so it is safe; freezing it against falling still stops it drifting
        # *down* into the swing (which would leak dark flashes back through).
        if not limiting or field > self._ema:
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
        """Desaturate a rapidly-changing saturated-red field toward white.

        Saturated red carries a stricter flash threshold, so rapidly swinging red
        is pulled toward white (which both lowers the red-flash risk and softens
        the look) without dimming the field. Driven by the raw-field *activity*
        plus any registered flashes — the brightness limiter may already be
        pinning the field, but the red content is still swinging underneath.
        """
        redness = _field_redness(colors)
        if redness < RED_SATURATION:
            return colors
        over = max(0, len(self._flashes) - MAX_RED_FLASHES_PER_S)
        # Strength from over-budget flashes plus how hard the field is swinging.
        drive = 0.15 * over + max(0.0, self._activity - _ACTIVITY_CALM)
        mix = min(0.5, drive) * redness
        if mix <= 0.01:
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
