"""Per-song light-timing auto-calibration.

The live tap anchors its real-time clock at track start, then the decoder spins
up for a *variable* amount of time — so some songs begin with a small residual
offset between the analysed audio and what the speakers actually play (the
"startup hang" the user otherwise trims by hand). This estimates that offset and
holds it for the track.

The signal is how much the analyser's playhead (``analyzer_pos``) leads the
player's reported audible position (``audible_pos``) *beyond the seek-ahead the
source already intends* (``expected_lead_s``). In the no-hang case the analyser
leads by exactly that intended amount, so the measured deviation is ~0 and the
working baseline delay is left untouched. When the decoder stalls at startup its
paced playhead slips ahead, the deviation grows, and that surplus is the extra
delay to apply — exactly the manual trim, found automatically. It is
deliberately robust (median, outlier rejection) because some players report a
jumpy ``media_position``; noisy data simply settles the correction near 0.

Pure logic, no Home Assistant imports, so the settling / locking / clamping is
unit-tested directly. This only chooses a *delay*; it never changes how a frame
is rendered.
"""

from __future__ import annotations

from collections import deque

from .const import TIMING_BUFFER_MS

# Settle over the first few seconds of steady playback, then lock for the track.
_SETTLE_S = 4.0
# Force a lock even if the estimate stays noisy, so a jumpy player still ends up
# on a stable value instead of drifting all song.
_HARD_LOCK_S = 8.0
_MIN_SAMPLES = 10          # need this many before a normal (spread-gated) lock
_PROVISIONAL_MIN = 3       # emit a provisional value this early so the start aligns
# Reject a raw sample this far from the running median (a position glitch / seek).
_OUTLIER_MS = 400.0
# Lock once the sample spread (MAD) is under this — or under ~this fraction of a
# beat when a track map supplies the beat period (a musically-meaningful bound).
_LOCK_SPREAD_MS = 35.0
_LOCK_BEAT_FRAC = 0.15
# Applied-delay clamp. Delaying (analysis leads sound — the startup-hang case) is
# unbounded; advancing is only possible within the baseline delay buffer.
_CLAMP_LO_MS = -TIMING_BUFFER_MS
_CLAMP_HI_MS = 400
_MAX_SAMPLES = 320         # ~settle window at 50 fps, bounded


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _mad(values: list[float], centre: float) -> float:
    """Median absolute deviation — a robust spread that ignores outliers."""
    return _median([abs(v - centre) for v in values])


class TimingCalibrator:
    """Estimate and lock the per-song light delay from the analyser/sound gap."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget the current track (call on every track change)."""
        self._samples: deque[float] = deque(maxlen=_MAX_SAMPLES)
        self._elapsed = 0.0          # seconds of steady playback sampled
        self._value: float | None = None  # current applied offset (ms), or None
        self._locked = False

    @property
    def offset_ms(self) -> int | None:
        """The delay to apply (ms), or ``None`` before a value exists."""
        return None if self._value is None else int(round(self._value))

    @property
    def locked(self) -> bool:
        return self._locked

    def update(
        self,
        dt: float,
        *,
        analyzer_pos: float | None,
        audible_pos: float | None,
        expected_lead_s: float,
        playing: bool,
        beat_period_s: float | None = None,
    ) -> None:
        """Feed one frame. Holds its value once locked or when data is missing.

        ``expected_lead_s`` is the seek-ahead the source already intends (so the
        no-hang deviation is ~0); the resulting offset is a *correction added to*
        the existing baseline delay, never a replacement for it.
        """
        if self._locked:
            return
        if not playing or analyzer_pos is None or audible_pos is None:
            return

        # Surplus lead beyond the intended seek-ahead: the startup slippage.
        raw = (analyzer_pos - audible_pos - expected_lead_s) * 1000.0
        if self._samples:
            centre = _median(list(self._samples))
            if abs(raw - centre) > _OUTLIER_MS:
                return  # a position glitch / seek — don't poison the estimate
        self._samples.append(raw)
        self._elapsed += max(0.0, dt)

        if len(self._samples) < _PROVISIONAL_MIN:
            return

        vals = list(self._samples)
        centre = _median(vals)
        self._value = _clamp(centre)  # provisional: applied immediately

        # Normal lock: enough settled samples AND a tight spread.
        spread_gate = _LOCK_SPREAD_MS
        if beat_period_s and beat_period_s > 0:
            spread_gate = max(_LOCK_SPREAD_MS, _LOCK_BEAT_FRAC * beat_period_s * 1000.0)
        settled = self._elapsed >= _SETTLE_S and len(self._samples) >= _MIN_SAMPLES
        if settled and _mad(vals, centre) <= spread_gate:
            self._value = _clamp(centre)
            self._locked = True
        elif self._elapsed >= _HARD_LOCK_S:
            # Noisy player: stop chasing it and commit the best estimate we have.
            self._value = _clamp(centre)
            self._locked = True


def _clamp(ms: float) -> float:
    return max(_CLAMP_LO_MS, min(_CLAMP_HI_MS, ms))
