"""The player's media timeline, projected onto ``time.monotonic()``.

The light show needs to know *where in the song the speakers are* at any
instant. Home Assistant only offers ``media_position`` plus the wall time it was
last written, refreshed on seek / play-pause / track change and nothing else —
so everything between those pushes has to be modelled, not read.

The old approach modelled it with a proportional PLL inside the track-map source:
an internal counter free-running at exactly 1.0x, tugged toward the reported
position every 0.5 s. Three things go wrong with that, and all three are audible:

* it advanced off a *frame counter*, so whenever the render loop fell behind,
  the timeline fell behind with it — and the resulting lag settled just under
  the snap threshold, where it became permanent and invisible;
* being proportional-only it has no rate state, so any player-vs-host clock skew
  leaves a standing offset it can never remove;
* its correction time constant was ~3 s, so a 250 ms player glitch took nearly
  8 s to disappear.

This module replaces it with an anchored clock: ``position = anchor + rate x
elapsed``, where elapsed comes from the monotonic clock (immune to loop
congestion) and ``rate`` is *estimated* (so skew is removed rather than
tolerated). It is correct from a **single** observation — extra observations only
remove accumulated error and refine the rate — which matters because Sonos and
DLNA push ``media_position_updated_at`` only on seek and track change. A design
needing N samples before it could be trusted would be broken on exactly those
players.

Corrections are applied in two regimes. A confirmed jump re-anchors at once and
raises a discontinuity, because when the *music* jumped a light jump is the
correct response. Anything smaller is slewed in below the visibility threshold.

:class:`ClockFilter` is the other half: Sendspin (which Music Assistant serves)
carries a microsecond server clock and stamps its progress reports against it, so
when that feed is available the anchor is exact rather than inferred. The filter
is what converts server timestamps to the local monotonic timeline.

Pure logic, no Home Assistant imports, so all of this is unit-tested directly.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime
from typing import Final

_LOGGER = logging.getLogger(__name__)

# -- confidence levels --------------------------------------------------------

CONFIDENCE_NONE: Final = "none"      # never anchored, or the player went away
CONFIDENCE_COARSE: Final = "coarse"  # anchored, but rate unproven / just jumped
CONFIDENCE_GOOD: Final = "good"      # anchored, rate estimated, residual small


# =============================================================================
# Sendspin server-clock synchronisation
# =============================================================================

# Constants below are the Sendspin reference implementation's
# (``aiosendspin/client/time_sync.py``); using its values rather than inventing
# our own keeps synco behaving like every other client on the same server.

_MAX_ERROR_SCALE: Final = 0.5
"""Measurement sigma as a fraction of the exchange's unattributable round trip."""

_ADAPTIVE_FORGETTING_CUTOFF: Final = 3.0
"""Inflate covariance when a residual exceeds this many ``max_error``."""

_FORGET_FACTOR: Final = 2.0
"""Covariance inflation (squared) applied when the cutoff above is exceeded."""

_DRIFT_SIGNIFICANCE_SQ: Final = 4.0
"""Only apply drift once ``drift^2 > this x drift_covariance`` (a 2-sigma gate),
so an unconverged drift estimate cannot run the clock away."""

_PROCESS_STD: Final = 0.0
_DRIFT_PROCESS_STD: Final = 1e-11


class ClockFilter:
    """Two-dimensional Kalman filter tracking clock offset *and* drift.

    Fed the four timestamps of an NTP-style exchange, it maintains the mapping
    between the local monotonic clock and a remote monotonic clock. Drift is
    what lets the estimate stay good between exchanges, so the poll can be slow.

    All times are seconds; convert Sendspin's microseconds before feeding it.
    """

    __slots__ = (
        "_offset", "_drift", "_p_off", "_p_drift", "_p_cross",
        "_last_t", "_n", "_prev_measurement", "_prev_t",
    )

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._offset = 0.0
        self._drift = 0.0
        self._p_off = math.inf
        self._p_drift = math.inf
        self._p_cross = 0.0
        self._last_t: float | None = None
        self._n = 0
        self._prev_measurement: float | None = None
        self._prev_t: float | None = None

    @property
    def ready(self) -> bool:
        """True once the mapping can be trusted (>= 2 exchanges, finite error)."""
        return self._n >= 2 and math.isfinite(self._p_off)

    @property
    def offset(self) -> float:
        return self._offset

    @property
    def drift(self) -> float:
        return self._drift

    @property
    def error(self) -> float:
        """Current 1-sigma uncertainty of the offset estimate, in seconds."""
        return math.inf if not math.isfinite(self._p_off) else math.sqrt(self._p_off)

    def _effective_drift(self) -> float:
        """Drift, gated on being statistically significant (2 sigma)."""
        if not math.isfinite(self._p_drift):
            return 0.0
        if self._drift * self._drift <= _DRIFT_SIGNIFICANCE_SQ * self._p_drift:
            return 0.0
        return self._drift

    def update(
        self,
        *,
        client_transmitted: float,
        server_received: float,
        server_transmitted: float,
        client_received: float,
    ) -> None:
        """Fold in one completed exchange (all four timestamps, in seconds)."""
        t1, t2, t3, t4 = (
            client_transmitted, server_received, server_transmitted, client_received
        )
        # Offset is the mean of the two one-way skews; max_error is half the
        # round trip we cannot attribute to either direction.
        measurement = ((t2 - t1) + (t3 - t4)) / 2.0
        max_error = max(0.0, ((t4 - t1) - (t3 - t2)) / 2.0)
        now = t4

        if self._n == 0:
            self._offset = measurement
            self._p_off = max(max_error * max_error, 1e-12)
            self._p_drift = 1.0
            self._p_cross = 0.0
            self._last_t = now
            self._prev_measurement = measurement
            self._prev_t = now
            self._n = 1
            return

        if self._n == 1 and self._prev_t is not None and now > self._prev_t:
            # Bootstrap drift by finite difference so the filter does not have
            # to discover the slope from scratch.
            span = now - self._prev_t
            self._drift = (measurement - self._prev_measurement) / span
            self._p_drift = max((2.0 * max_error / span) ** 2, 1e-18)

        dt = 0.0 if self._last_t is None else max(0.0, now - self._last_t)
        self._last_t = now

        # -- predict ---------------------------------------------------------
        off_pred = self._offset + self._drift * dt
        p_off = self._p_off + 2.0 * dt * self._p_cross + dt * dt * self._p_drift
        p_cross = self._p_cross + dt * self._p_drift
        p_drift = self._p_drift
        p_off += _PROCESS_STD * _PROCESS_STD
        p_drift += _DRIFT_PROCESS_STD * _DRIFT_PROCESS_STD * dt

        residual = measurement - off_pred

        # -- adaptive forgetting ---------------------------------------------
        # A residual far outside the exchange's own error bar means the model
        # has fallen behind reality (a network stall, a server clock step).
        # Inflating covariance lets the update below move much further, so the
        # filter recovers in one or two exchanges instead of a dozen.
        if max_error > 0.0 and abs(residual) > _ADAPTIVE_FORGETTING_CUTOFF * max_error:
            f2 = _FORGET_FACTOR * _FORGET_FACTOR
            p_off *= f2
            p_cross *= f2
            p_drift *= f2

        # -- update ----------------------------------------------------------
        meas_var = max((max_error * _MAX_ERROR_SCALE) ** 2, 1e-12)
        uncertainty = 1.0 / max(p_off + meas_var, 1e-9)
        k_off = p_off * uncertainty
        k_drift = p_cross * uncertainty

        self._offset = off_pred + k_off * residual
        self._drift += k_drift * residual
        self._p_off = p_off - k_off * p_off
        self._p_cross = p_cross - k_off * p_cross
        self._p_drift = p_drift - k_drift * p_cross
        self._n += 1

    def to_server(self, client_time: float) -> float:
        """Local monotonic seconds -> remote clock seconds."""
        base = self._last_t if self._last_t is not None else client_time
        return client_time + self._offset + self._effective_drift() * (client_time - base)

    def to_client(self, server_time: float) -> float:
        """Remote clock seconds -> local monotonic seconds."""
        drift = self._effective_drift()
        base = self._last_t if self._last_t is not None else 0.0
        return (server_time - self._offset + drift * base) / (1.0 + drift)


# =============================================================================
# The media-timeline clock
# =============================================================================

_SLEW_MAX: Final = 0.10
"""Seconds of correction absorbed per second of real time.

The single load-bearing constant. Because it is < 1.0 the projected position is
strictly increasing at 0.90x-1.10x real time *even while correcting backwards*,
and :meth:`~..trackmap.TrackMap.frame_at` fires each beat exactly once in the
half-open window ``(prev_pos, pos]`` — so forward slew never loses a beat (the
window merely widens) and backward slew never re-fires one (the query never
reverses). At 10 % the ~50 fps analysis stream is time-warped by at most one
frame per 200 ms, far below perception for a lighting envelope. It is also
larger than ``_RATE_HI - 1.0``, so the corrector always has more authority than
a maximally-wrong rate estimate and the loop cannot run away."""

_SEEK_S: Final = 0.75
"""Single-observation re-anchor threshold: below any plausible seek (a seek is
seconds; a skip drops to ~0 from mid-track), above any plausible jitter."""

_JUMP_S: Final = 0.12
"""Visible-but-ambiguous band. AV asynchrony becomes noticeable around 50-100 ms,
so 120 ms (six analysis frames) is unambiguous — but it is also reachable by one
bad report, hence the confirmation rule."""

_CONFIRM_TOL_S: Final = 0.08
"""Two consecutive errors must agree within this to count as a confirmed jump."""

_CORR_MAX_S: Final = 0.75
"""Cap on pending correction; equals ``_SEEK_S`` because anything larger is a
discontinuity rather than something to correct."""

_CORR_DEADBAND_S: Final = 0.004
"""A fifth of a 20 ms analysis frame — below one frame the correction is
unobservable, so zeroing it stops endless micro-slewing and keeps the rate
estimator from chasing quantisation."""

_RATE_MIN_OBS: Final = 5
_RATE_MIN_SPAN_S: Final = 20.0
"""Least squares needs both: +-50 ms of report noise over 20 s is only +-0.25 %
of slope error, but five reports inside one second would produce garbage."""

_RATE_GAIN: Final = 0.25
"""First-order pull toward each new slope estimate: fully adopted over ~4
updates, so the rate adapts without ever stepping."""

_RATE_LO: Final = 0.96
_RATE_HI: Final = 1.04
"""Real crystal skew is a few hundred ppm, so this is >100x any legitimate
value. It exists only to bound pathological cases (a resampling player, an
undisciplined host clock) — and stays inside ``_SLEW_MAX``."""

_STALE_S: Final = 20.0
"""Sonos polls about every 10 s, so this is 2x headroom. Stale does *not* mean
unusable: free-running from a good anchor at the estimated rate is exactly
right. It only surfaces in diagnostics and freezes the timing calibrator."""

_MAX_AGE_S: Final = 30.0
"""Clamp on ``now_wall - updated_at``. The 0 floor absorbs an ``updated_at``
slightly in the future (media-server vs HA clock skew); the ceiling makes a wall
clock *step* (NTP correction, DST, VM resume) degrade to "anchor at now" instead
of throwing the timeline an hour off. This is the only place wall time touches
the model — everything downstream is pure monotonic."""

_DISC_SETTLE_S: Final = 1.0
_RESIDUAL_ALPHA: Final = 0.25
_STEP_DT_MAX: Final = 0.25
_MAX_OBS: Final = 24


class PlaybackClock:
    """Where the speakers are in the song, on the local monotonic timeline."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._entity_id: str | None = None
        self.reset()

    # -- lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        self._anchor_pos = 0.0
        self._anchor_t: float | None = None
        self._rate = 1.0
        self._rate_known = False
        self._corr = 0.0
        self._last_step_t: float | None = None
        self._last_updated_at: datetime | None = None
        self._last_obs_t = 0.0
        self._obs: deque[tuple[float, float]] = deque(maxlen=_MAX_OBS)
        self._prev_err: float | None = None
        self._residual_ema: float | None = None
        self._track_key: str | None = None
        self._playing = False
        self._frozen_pos: float | None = None
        self._disc_pending = False
        self._last_disc_t = 0.0
        self._last_reason = ""
        self._provisional = False
        self._authoritative = False
        self._track_seq = 0
        self._anchor_seq = 0
        self._disc_seq = 0

    def rebind(self, entity_id: str | None) -> None:
        """Follow a different player (or none). Idempotent."""
        if entity_id == self._entity_id:
            return
        self._entity_id = entity_id
        self.reset()

    @property
    def entity_id(self) -> str | None:
        return self._entity_id

    def set_authoritative(self, on: bool) -> None:
        """Declare whether an exact feed is currently driving this clock.

        While it is, :meth:`observe` stops anchoring: Home Assistant's estimate
        would only add noise to a playhead the server has stated outright. It
        keeps doing its other job — track identity and play/pause — because
        those come from the followed entity either way.

        Arbitration lives here rather than at the call sites because there is
        more than one of them (the coordinator's state listener and the track
        map source's safety-net poll), and a feed that half of them ignored
        would be a feed that quietly fought itself.
        """
        if not on:
            self._authoritative = False

    def note_unavailable(self) -> None:
        """The player vanished: hold the timeline but stop trusting it."""
        self._playing = False
        self._frozen_pos = None
        self._anchor_t = None
        self._prev_err = None
        self._obs.clear()

    # -- ingest --------------------------------------------------------------

    def anchor(
        self,
        pos_s: float,
        at_mono: float,
        *,
        rate: float = 1.0,
        reason: str = "anchor",
        track_key: str | None = None,
    ) -> None:
        """Set an *authoritative* anchor (an exact, timestamped playhead).

        Used by the Sendspin feed, whose progress reports carry the server clock
        time at which they are valid, so there is nothing to infer or smooth.
        A rate of 0 means paused (Sendspin's ``playback_speed``).

        A repeat anchor that merely confirms where we already are is re-seated
        silently. Only one that actually *moves* the playhead announces a
        discontinuity — otherwise a server that re-sends progress freely would
        keep resetting every consumer's beat window and mute the show.
        """
        moved = True
        if track_key is not None and track_key != self._track_key:
            self._track_key = track_key
            self._track_seq += 1
        elif self._playing and self._anchor_t is not None:
            moved = abs(pos_s - self._projected(at_mono)) >= _JUMP_S
        self._authoritative = True
        if rate <= 0.0:
            if self._playing:
                self._raise_discontinuity(at_mono, reason)
            self._freeze(pos_s)
            return
        was_playing = self._playing
        self._playing = True
        self._frozen_pos = None
        self._rate = min(_RATE_HI, max(_RATE_LO, rate))
        self._rate_known = True
        if moved or not was_playing:
            self._hard_anchor(pos_s, at_mono, reason)
        else:
            # Exact feed, unmoved: take the position verbatim without telling
            # anyone the timeline jumped.
            self._anchor_pos = pos_s
            self._anchor_t = at_mono
            self._corr = 0.0
            self._prev_err = None
        self._last_obs_t = at_mono
        self._anchor_seq += 1

    def observe(
        self,
        *,
        state: str | None,
        media_position: float | None,
        updated_at: datetime | None,
        track_key: str | None,
        now_mono: float,
        now_wall: datetime,
    ) -> bool:
        """Fold in one Home Assistant player report.

        Returns True when this was a genuinely *new* observation. Repeats of the
        same ``updated_at`` are ignored: the position they imply is our own
        arithmetic, not the player's, so counting them as samples would turn one
        measurement into fifty identical ones — which is precisely how the old
        calibrator convinced itself a guess was a tight, well-agreed estimate.

        Safe to call from both the state listener and the polling safety net.
        """
        if state is None or state not in ("playing", "paused"):
            self.note_unavailable()
            return False

        if track_key != self._track_key:
            self._on_track_change(track_key, media_position, now_mono, state)
            # Fall through: if this report carries a fresh updated_at it is a
            # genuine anchor for the new song and should be taken now.

        if updated_at is not None and updated_at == self._last_updated_at:
            return False
        self._last_updated_at = updated_at

        if self._authoritative:
            # An exact feed owns the playhead; see set_authoritative().
            if state == "paused" and self._playing:
                self._freeze(media_position)
            return False

        if media_position is None:
            return False
        pos = float(media_position)

        # Place the report on the monotonic timeline at the instant it was true.
        if updated_at is None:
            t_obs = now_mono
        else:
            try:
                age = (now_wall - updated_at).total_seconds()
            except (TypeError, ValueError):
                age = 0.0
            t_obs = now_mono - min(_MAX_AGE_S, max(0.0, age))

        if state == "paused":
            self._freeze(pos)
            self._last_obs_t = now_mono
            self._anchor_seq += 1
            return True

        if not self._playing or self._anchor_t is None or self._provisional:
            was_provisional = self._provisional
            self._provisional = False
            self._playing = True
            self._frozen_pos = None
            self._hard_anchor(pos, t_obs, "resume" if not was_provisional else "track")
            self._last_obs_t = now_mono
            self._anchor_seq += 1
            return True

        err = pos - self._projected(t_obs)
        self._residual_ema = (
            abs(err) if self._residual_ema is None
            else self._residual_ema + _RESIDUAL_ALPHA * (abs(err) - self._residual_ema)
        )

        if abs(err) >= _SEEK_S:
            self._hard_anchor(pos, t_obs, "seek")
        elif (
            abs(err) >= _JUMP_S
            and self._prev_err is not None
            and abs(self._prev_err) >= _JUMP_S
            and (err > 0) == (self._prev_err > 0)
            and abs(err - self._prev_err) <= _CONFIRM_TOL_S
        ):
            # Two independent reports agree that we are visibly off: real.
            self._hard_anchor(pos, t_obs, "jump")
        else:
            self._prev_err = err
            # Assign, never accumulate: ``err`` is measured against the already
            # partially-corrected output, so it is by definition what remains.
            self._corr = min(_CORR_MAX_S, max(-_CORR_MAX_S, err))
            self._obs.append((t_obs, pos))
            self._update_rate(t_obs)

        self._last_obs_t = now_mono
        self._anchor_seq += 1
        return True

    # -- run -----------------------------------------------------------------

    def step(self, now_mono: float) -> None:
        """Absorb pending correction. Call once per render frame."""
        last, self._last_step_t = self._last_step_t, now_mono
        if last is None or not self._playing or self._anchor_t is None:
            return
        if abs(self._corr) <= _CORR_DEADBAND_S:
            self._corr = 0.0
            return
        # Clamp dt so a long stall cannot authorise one huge jump.
        dt = min(_STEP_DT_MAX, max(0.0, now_mono - last))
        limit = _SLEW_MAX * dt
        move = min(limit, max(-limit, self._corr))
        self._anchor_pos += move
        self._corr -= move

    def position(self, now_mono: float) -> float | None:
        """Media-timeline position in seconds, or None when unusable."""
        if not self._playing:
            return self._frozen_pos
        if self._anchor_t is None:
            return None
        return self._anchor_pos + self._rate * (now_mono - self._anchor_t)

    def take_discontinuity(self) -> bool:
        """True exactly once after each re-anchor. Single-consumer.

        Other consumers should watch :attr:`disc_seq` instead.
        """
        was, self._disc_pending = self._disc_pending, False
        return was

    # -- introspection -------------------------------------------------------

    @property
    def confidence(self) -> str:
        if self._anchor_t is None and self._frozen_pos is None:
            return CONFIDENCE_NONE
        if not self._rate_known or self._last_step_t is None:
            return CONFIDENCE_COARSE
        if self._last_step_t - self._last_disc_t < _DISC_SETTLE_S:
            return CONFIDENCE_COARSE
        if self._residual_ema is not None and self._residual_ema > _JUMP_S:
            return CONFIDENCE_COARSE
        return CONFIDENCE_GOOD

    @property
    def authoritative(self) -> bool:
        """True when the anchor came from an exact feed (Sendspin), not HA state."""
        return self._authoritative

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def residual_ms(self) -> float | None:
        return None if self._residual_ema is None else self._residual_ema * 1000.0

    @property
    def pending_ms(self) -> float:
        return self._corr * 1000.0

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def stale(self) -> bool:
        if self._last_step_t is None or self._last_obs_t == 0.0:
            return False
        return (self._last_step_t - self._last_obs_t) > _STALE_S

    @property
    def track_key(self) -> str | None:
        return self._track_key

    @property
    def track_seq(self) -> int:
        return self._track_seq

    @property
    def anchor_seq(self) -> int:
        return self._anchor_seq

    @property
    def disc_seq(self) -> int:
        return self._disc_seq

    # -- internals -----------------------------------------------------------

    def _projected(self, t_mono: float) -> float:
        return self._anchor_pos + self._rate * (t_mono - self._anchor_t)

    def _hard_anchor(self, pos: float, at_mono: float, reason: str) -> None:
        if self._anchor_t is not None and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "[%s] clock re-anchor (%s): err=%+.0f ms -> pos=%.2f rate=%.4f",
                self.name, reason,
                (pos - self._projected(at_mono)) * 1000.0, pos, self._rate,
            )
        self._anchor_pos = pos
        self._anchor_t = at_mono
        self._corr = 0.0
        self._prev_err = None
        # The observation window now spans a discontinuity, so it can no longer
        # be regressed for a rate. The rate estimate itself survives: the
        # player's crystal did not change, only our place in the song did.
        self._obs.clear()
        self._raise_discontinuity(at_mono, reason)

    def _raise_discontinuity(self, at_mono: float, reason: str) -> None:
        self._disc_pending = True
        self._disc_seq += 1
        self._last_disc_t = at_mono
        self._last_reason = reason

    def _freeze(self, pos: float | None) -> None:
        if pos is not None:
            self._frozen_pos = pos
        elif self._frozen_pos is None and self._anchor_t is not None:
            self._frozen_pos = self._anchor_pos
        self._playing = False
        self._corr = 0.0
        self._prev_err = None
        self._obs.clear()

    def _on_track_change(
        self,
        track_key: str | None,
        media_position: float | None,
        now_mono: float,
        state: str,
    ) -> None:
        """A new song: invalidate the anchors, keep the rate.

        Deliberately does *not* extrapolate from ``updated_at`` here. An
        integration that writes the new title before the new position would
        otherwise seat the new song at the old song's playhead — the lights
        starting a song that has not started yet. Anchor provisionally at the
        bare reported position (a fresh track reports ~0) and wait for a genuine
        report to take over.
        """
        self._track_key = track_key
        self._track_seq += 1
        self._obs.clear()
        self._corr = 0.0
        self._prev_err = None
        self._residual_ema = None
        self._provisional = True
        self._frozen_pos = None
        self._anchor_pos = float(media_position) if media_position is not None else 0.0
        self._anchor_t = now_mono
        self._playing = state == "playing"
        self._raise_discontinuity(now_mono, "track")

    def _update_rate(self, at_mono: float) -> None:
        """Least-squares slope of reported position against monotonic time.

        The anchor is re-seated at the current projection before the slope
        changes, so the output stays *continuous* across the update. Without
        that, a rate nudge would instantly shift the position by
        ``delta_rate x (now - anchor_t)`` — seconds after anchoring, that is a
        visible step, and a downward one would rewind the beat query.
        """
        if len(self._obs) < _RATE_MIN_OBS:
            return
        t0 = self._obs[0][0]
        span = self._obs[-1][0] - t0
        if span < _RATE_MIN_SPAN_S:
            return
        n = float(len(self._obs))
        sx = sy = sxx = sxy = 0.0
        for t, p in self._obs:
            x = t - t0
            sx += x
            sy += p
            sxx += x * x
            sxy += x * p
        denom = n * sxx - sx * sx
        if denom <= 0.0:
            return
        slope = (n * sxy - sx * sy) / denom
        slope = min(_RATE_HI, max(_RATE_LO, slope))
        rate = self._rate + _RATE_GAIN * (slope - self._rate)
        rate = min(_RATE_HI, max(_RATE_LO, rate))
        if rate != self._rate:
            if abs(rate - self._rate) > 0.002:
                _LOGGER.debug(
                    "[%s] clock rate -> %.4f (n=%d span=%.0fs)",
                    self.name, rate, len(self._obs), span,
                )
            self._anchor_pos = self._projected(at_mono)
            self._anchor_t = at_mono
            self._rate = rate
        self._rate_known = True
