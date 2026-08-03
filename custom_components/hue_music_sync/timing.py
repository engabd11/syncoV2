"""Per-song light-timing calibration and the applied-delay slew limiter.

The live Music Assistant tap anchors its real-time clock at track start, then
the decoder spins up for a *variable* amount of time — so some songs begin with
a small residual offset between the analysed audio and what the speakers
actually play (the "startup hang" the user otherwise trims by hand). The
calibrator estimates that offset.

The signal is how much the analyser's playhead leads the audible position
*beyond the seek-ahead the source already intends*. In the no-hang case the
analyser leads by exactly that intended amount, the deviation is ~0, and the
working baseline delay is left untouched.

Two properties are load-bearing and easy to lose:

**One sample per player report, never per render frame.** The player's position
between reports is our own arithmetic, not new information. Sampling it at 50 fps
turns a single measurement into fifty identical ones, which produces a beautifully
tight median-and-MAD around a number that may simply be wrong — the estimator
convinces itself a guess is a well-agreed estimate. Callers must feed
:meth:`~TimingCalibrator.observe` only when the player genuinely reported
something new; :meth:`~TimingCalibrator.tick` carries wall time separately.

**Nothing locks permanently.** The committed value is held through a hysteresis
band so it does not twitch, but it can always re-open: tracks change, players
seek, and a value that was right for song one has no claim on song two.

Pure logic, no Home Assistant imports, so the settling, robustness and clamping
are unit-tested directly. This only ever chooses a *delay*; it never changes how
a frame is rendered.
"""

from __future__ import annotations

from collections import deque
from typing import Final

from .const import TIMING_BUFFER_MS

_MIN_OBS: Final = 4
"""A median of four rejects one outlier — the smallest honestly robust set."""

_MIN_SPAN_S: Final = 2.0
"""...and they must not all arrive in one burst, or the "spread" is meaningless."""

_PROVISIONAL_OBS: Final = 2
"""Emit a value this early so the *start* of a track is already roughly right."""

_OUTLIER_MS: Final = 400.0
"""Reject a sample this far from the running median (a position glitch/seek)."""

_HYST_MS: Final = 25.0
"""Re-commit only when the estimate has moved enough to *see*: below the ~50 ms
audio-visual asynchrony floor, above the 20 ms analysis frame period."""

_LOCK_BEAT_FRAC: Final = 0.15
"""When a track map supplies the beat period, judge spread against a musically
meaningful bound rather than a fixed millisecond count."""

_MAX_OBS: Final = 24
"""A rolling window of real reports (~24 s on a player reporting at 1 Hz)."""

_DEFAULT_CLAMP_HI_MS: Final = 400.0


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _mad(values: list[float], centre: float) -> float:
    """Median absolute deviation — a robust spread that ignores outliers."""
    return _median([abs(v - centre) for v in values])


def slew_toward(current: float | None, target: float, dt: float, rate: float) -> float:
    """Move ``current`` toward ``target`` by at most ``rate`` per second.

    ``None`` adopts the target immediately, which is what a genuine
    discontinuity wants (nothing buffered to protect). ``dt`` is clamped so a
    stalled loop cannot authorise one large step.
    """
    if current is None:
        return target
    step = rate * min(0.25, max(0.0, dt))
    delta = target - current
    return current + max(-step, min(step, delta))


class TimingCalibrator:
    """Estimate the source's residual analysis-lead error, in milliseconds."""

    def __init__(
        self,
        *,
        clamp_lo_ms: float = -TIMING_BUFFER_MS,
        clamp_hi_ms: float = _DEFAULT_CLAMP_HI_MS,
    ) -> None:
        self._lo = float(clamp_lo_ms)
        self._hi = float(clamp_hi_ms)
        self.reset()

    def reset(self) -> None:
        """Forget the current track. Called on every track change and seek."""
        self._obs: deque[tuple[float, float]] = deque(maxlen=_MAX_OBS)  # (t, dev_ms)
        self._elapsed = 0.0
        self._value: float | None = None
        self._settled = False

    def set_clamp(self, lo_ms: float, hi_ms: float) -> None:
        """Bound the correction to what the delay buffer can actually apply.

        Advancing is only possible within the baseline delay, which varies by
        source, so the caller supplies it rather than assuming one value.
        """
        self._lo, self._hi = float(lo_ms), float(hi_ms)
        if self._value is not None:
            self._value = self._clamp(self._value)

    # -- state ---------------------------------------------------------------

    @property
    def offset_ms(self) -> int | None:
        """The correction to apply (ms), or None before a value exists."""
        return None if self._value is None else int(round(self._value))

    @property
    def settled(self) -> bool:
        """True once the estimate is stable enough to present as a number."""
        return self._settled

    # The card reads `timing_locked`; the name is kept, the meaning is now
    # "settled" rather than "locked forever".
    locked = settled

    @property
    def samples(self) -> int:
        return len(self._obs)

    @property
    def spread_ms(self) -> float | None:
        if len(self._obs) < _MIN_OBS:
            return None
        vals = [d for _, d in self._obs]
        return _mad(vals, _median(vals))

    # -- feeding -------------------------------------------------------------

    def tick(self, dt: float) -> None:
        """Advance the settle clock with the honest wall ``dt``."""
        self._elapsed += max(0.0, dt)

    def observe(self, deviation_ms: float, *, beat_period_s: float | None = None) -> None:
        """Fold in one genuinely independent measurement."""
        if self._obs:
            centre = _median([d for _, d in self._obs])
            if abs(deviation_ms - centre) > _OUTLIER_MS:
                return  # a position glitch / seek — don't poison the estimate
        self._obs.append((self._elapsed, deviation_ms))

        n = len(self._obs)
        if n < _PROVISIONAL_OBS:
            return
        vals = [d for _, d in self._obs]
        centre = _median(vals)

        if self._value is None:
            self._value = self._clamp(centre)  # provisional: applied at once
            return

        span = self._obs[-1][0] - self._obs[0][0]
        if n < _MIN_OBS or span < _MIN_SPAN_S:
            return

        gate = _HYST_MS
        if beat_period_s and beat_period_s > 0:
            gate = max(_HYST_MS, _LOCK_BEAT_FRAC * beat_period_s * 1000.0)
        # Move only when the change would be visible; otherwise hold, so a
        # steady estimate does not twitch the show every report.
        if abs(centre - self._value) > gate:
            self._value = self._clamp(centre)
        self._settled = _mad(vals, centre) <= gate

    def _clamp(self, ms: float) -> float:
        return max(self._lo, min(self._hi, ms))
