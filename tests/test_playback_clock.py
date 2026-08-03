"""The playback clock: anchoring, correction, rate estimation, discontinuities.

These are the guarantees the light show depends on. The two that matter most and
are easy to lose in a refactor:

* the projected position is **monotonically increasing** even while correcting
  backwards (``TrackMap.frame_at`` fires each beat once in ``(prev, pos]``, so a
  reversing query would re-fire beats and a jumping one would drop them);
* a *single* bad report never steps the timeline, but two agreeing ones do.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from hue_music_sync.audio.clock import (
    CONFIDENCE_COARSE,
    CONFIDENCE_GOOD,
    CONFIDENCE_NONE,
    ClockFilter,
    PlaybackClock,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_DT = 0.02  # the 50 fps render period


def _wall(t: float) -> datetime:
    return _EPOCH + timedelta(seconds=t)


class Driver:
    """Runs a clock over a synthetic timeline of monotonic time + player reports."""

    def __init__(self, clock: PlaybackClock, track: str = "song-1") -> None:
        self.clock = clock
        self.t = 100.0  # a non-zero monotonic origin, as in real life
        self.track = track

    def report(
        self,
        pos: float,
        *,
        at: float | None = None,
        state: str = "playing",
        track: str | None = None,
    ) -> bool:
        """Deliver a player report stamped ``at`` (defaults to now)."""
        stamp = self.t if at is None else at
        return self.clock.observe(
            state=state,
            media_position=pos,
            updated_at=_wall(stamp),
            track_key=self.track if track is None else track,
            now_mono=self.t,
            now_wall=_wall(self.t),
        )

    def run(self, seconds: float, dt: float = _DT) -> None:
        """Advance the render loop without any new player reports."""
        n = int(round(seconds / dt))
        for _ in range(n):
            self.t += dt
            self.clock.step(self.t)

    def pos(self) -> float | None:
        return self.clock.position(self.t)


def _clock() -> tuple[PlaybackClock, Driver]:
    c = PlaybackClock("test")
    return c, Driver(c)


# -- anchoring ----------------------------------------------------------------


def test_single_anchor_runs_at_real_time():
    """One report is enough: the clock is correct by construction from it."""
    c, d = _clock()
    d.report(10.0)
    d.run(60.0)
    assert d.pos() == pytest.approx(70.0, abs=1e-3)


def test_unanchored_clock_has_no_position():
    c, d = _clock()
    assert d.pos() is None
    assert c.confidence == CONFIDENCE_NONE


def test_repeat_updated_at_is_ignored():
    """The extrapolated position between pushes is our arithmetic, not a sample."""
    c, d = _clock()
    d.report(10.0, at=100.0)
    seq = c.anchor_seq
    d.run(5.0)
    # Same updated_at, a later wall clock and a (wrong) position: no new info.
    assert d.report(999.0, at=100.0) is False
    assert c.anchor_seq == seq
    assert d.pos() == pytest.approx(15.0, abs=1e-3)


def test_updated_at_is_placed_on_the_monotonic_timeline():
    """A report that was true 2 s ago must already include those 2 s."""
    c, d = _clock()
    d.t = 100.0
    d.report(10.0, at=98.0)
    assert d.pos() == pytest.approx(12.0, abs=1e-3)


def test_future_updated_at_is_clamped():
    """Media-server vs HA clock skew must not rewind the timeline."""
    c, d = _clock()
    d.t = 100.0
    d.report(10.0, at=140.0)
    assert d.pos() == pytest.approx(10.0, abs=1e-3)


def test_absurd_age_is_clamped():
    """A wall-clock step (NTP/DST/VM resume) degrades to 'anchor at now'."""
    c, d = _clock()
    d.t = 100.0
    d.report(10.0, at=100.0 - 3600.0)
    assert d.pos() == pytest.approx(40.0, abs=1e-3)  # clamped to _MAX_AGE_S = 30


def test_unavailable_player_drops_the_position():
    c, d = _clock()
    d.report(10.0)
    d.run(1.0)
    d.report(0.0, state="off")
    assert d.pos() is None
    assert c.confidence == CONFIDENCE_NONE


# -- correction ---------------------------------------------------------------


def test_small_glitch_is_slewed_not_stepped():
    """80 ms is corrected smoothly, monotonically, and within about a second."""
    c, d = _clock()
    d.report(10.0)
    d.run(2.0)
    c.take_discontinuity()

    d.run(1.0)
    truth = d.pos() + 0.08
    assert d.report(truth) is True
    assert c.take_discontinuity() is False  # no jump announced

    prev = d.pos()
    for _ in range(60):  # 1.2 s
        d.t += _DT
        c.step(d.t)
        now = d.pos()
        delta = now - prev
        assert delta > 0.0, "the timeline must never reverse"
        assert 0.9 * _DT <= delta <= 1.1 * _DT + 1e-9
        prev = now
    assert d.pos() == pytest.approx(truth + 1.2, abs=0.005)


def test_single_bad_report_is_absorbed_not_stepped():
    """One outlier must never move the show; the next good report undoes it."""
    c, d = _clock()
    d.report(10.0)
    d.run(2.0)
    c.take_discontinuity()

    d.run(1.0)
    d.report(d.pos() + 0.30)  # a lone bad report
    assert c.take_discontinuity() is False
    d.run(1.0)
    d.report(d.pos() - 0.20)  # contradicted: different sign, not a confirmation
    assert c.take_discontinuity() is False


def test_confirmed_jump_steps_once():
    """Two agreeing reports are believed, and announce exactly one jump."""
    c, d = _clock()
    d.report(10.0)
    d.run(2.0)
    c.take_discontinuity()

    d.run(1.0)
    d.report(d.pos() + 0.30)
    assert c.take_discontinuity() is False
    d.run(1.0)
    truth = d.pos() + 0.30
    d.report(truth)
    assert c.take_discontinuity() is True
    assert c.take_discontinuity() is False
    assert d.pos() == pytest.approx(truth, abs=1e-3)


def test_seek_steps_immediately():
    """A real seek is unambiguous: one report is enough."""
    c, d = _clock()
    d.report(10.0)
    d.run(2.0)
    c.take_discontinuity()

    d.report(90.0)
    assert c.take_discontinuity() is True
    assert d.pos() == pytest.approx(90.0, abs=1e-3)


def test_correction_is_rate_limited_not_instant():
    """A 400 ms error takes multiple seconds, and never lands in one frame."""
    c, d = _clock()
    d.report(10.0)
    d.run(2.0)
    d.t += 1.0
    target = d.pos() + 0.40
    d.report(target)
    before = d.pos()
    d.t += _DT
    c.step(d.t)
    assert d.pos() - before < 0.05  # nowhere near a 400 ms jump


def test_step_clamps_a_long_stall():
    """A frozen event loop must not authorise one huge correction."""
    c, d = _clock()
    d.report(10.0)
    d.run(2.0)
    d.report(d.pos() + 0.5)
    before = d.pos()
    d.t += 5.0  # a five-second stall between step() calls
    c.step(d.t)
    # Only _STEP_DT_MAX (0.25 s) of slew is granted: 0.25 * 0.10 = 25 ms.
    assert (d.pos() - before) - 5.0 <= 0.026


# -- track changes and pausing ------------------------------------------------


def test_track_change_reanchors_and_ignores_a_stale_updated_at():
    """The new song must not inherit the old song's playhead."""
    c, d = _clock()
    d.report(120.0, at=100.0)
    d.run(30.0)
    seq = c.track_seq

    # The player announces the new title but has not yet written a position.
    d.report(0.0, at=100.0, track="song-2")
    assert c.track_seq == seq + 1
    assert c.take_discontinuity() is True
    assert d.pos() == pytest.approx(0.0, abs=1e-3)  # not 150 s


def test_track_change_then_a_real_report_takes_over():
    c, d = _clock()
    d.report(120.0, at=100.0)
    d.run(30.0)
    d.report(0.0, at=100.0, track="song-2")
    d.run(1.0)
    d.report(1.5, track="song-2")
    d.run(2.0)
    assert d.pos() == pytest.approx(3.5, abs=0.01)


def test_pause_freezes_and_resume_reanchors():
    c, d = _clock()
    d.report(10.0)
    d.run(5.0)
    d.report(15.0, state="paused")
    assert c.playing is False
    frozen = d.pos()
    d.run(10.0)
    assert d.pos() == pytest.approx(frozen, abs=1e-9), "paused must not advance"

    d.report(15.0)
    assert c.playing is True
    d.run(3.0)
    assert d.pos() == pytest.approx(18.0, abs=0.01)


# -- rate estimation ----------------------------------------------------------


def _skewed_run(clock: PlaybackClock, skew: float, seconds: float, every: float = 5.0):
    """Feed reports from a player whose clock runs at ``skew`` x real time."""
    d = Driver(clock)
    d.report(0.0)
    t = 0.0
    while t < seconds:
        d.run(every)
        t += every
        d.report(t * skew)
    return d


def test_rate_estimation_removes_a_skew():
    """A player running 1 % fast leaves no standing offset (the PLL's weakness)."""
    c = PlaybackClock("skew")
    d = _skewed_run(c, 1.01, 60.0)
    assert c.rate == pytest.approx(1.01, rel=0.02)
    # Free-run for another 30 s with no reports and stay close to the truth.
    before_t, before_pos = d.t, d.pos()
    d.run(30.0)
    expected = before_pos + 30.0 * 1.01
    assert d.pos() == pytest.approx(expected, abs=0.05)


def test_rate_is_clamped():
    c = PlaybackClock("wild")
    _skewed_run(c, 1.30, 60.0)
    assert c.rate <= 1.04 + 1e-9


def test_rate_needs_span_and_count():
    """Five reports inside a second must not be regressed into a rate."""
    c, d = _clock()
    d.report(0.0)
    for i in range(6):
        d.run(0.2)
        d.report(0.2 * (i + 1))
    assert c.rate == pytest.approx(1.0, abs=1e-9)


# -- confidence ---------------------------------------------------------------


def test_confidence_progression():
    c, d = _clock()
    assert c.confidence == CONFIDENCE_NONE
    d.report(0.0)
    d.run(0.5)
    assert c.confidence == CONFIDENCE_COARSE  # rate unproven
    d2 = _skewed_run(c := PlaybackClock("p"), 1.0, 60.0)
    d2.run(2.0)
    assert c.confidence == CONFIDENCE_GOOD


def test_stale_anchor_keeps_free_running():
    """No reports for a while is not a failure — it is Sonos behaving normally."""
    c, d = _clock()
    d.report(10.0)
    d.run(40.0)
    assert c.stale is True
    assert d.pos() == pytest.approx(50.0, abs=1e-3)


# -- the property that protects the beat grid ---------------------------------


def test_output_is_monotonic_under_adversarial_input():
    """Jitter, glitches and a seek: the query must only ever move forward.

    ``frame_at(pos, prev_pos)`` fires beats in ``(prev_pos, pos]``. A reversing
    query re-fires beats already played; that is the failure this guards.
    """
    rng = random.Random(20260803)
    c, d = _clock()
    d.report(0.0)
    prev = d.pos()
    truth = 0.0

    for i in range(3000):  # 60 s at 50 fps
        d.t += _DT
        c.step(d.t)
        truth += _DT
        if i % 50 == 0:  # a report about once a second
            jitter = rng.uniform(-0.05, 0.05)
            if i == 1500:
                truth += 40.0  # a seek
            elif i % 500 == 0:
                jitter += rng.choice((-0.3, 0.3))  # a lone glitchy report
            d.report(truth + jitter)
        now = d.pos()
        if c.take_discontinuity():
            prev = now  # a genuine jump: the show is meant to snap
            continue
        assert now >= prev - 1e-12, f"timeline reversed at frame {i}"
        prev = now


# -- the authoritative (Sendspin) feed ----------------------------------------


def test_authoritative_anchor_is_exact_and_silent_when_unmoved():
    """A repeat progress report must not keep announcing discontinuities."""
    c = PlaybackClock("sendspin")
    c.anchor(30.0, 100.0, rate=1.0, reason="sendspin", track_key="s1")
    assert c.take_discontinuity() is True
    c.step(101.0)
    assert c.position(105.0) == pytest.approx(35.0, abs=1e-9)

    c.anchor(35.0, 105.0, rate=1.0, reason="sendspin", track_key="s1")
    assert c.take_discontinuity() is False, "unmoved anchor must stay quiet"
    assert c.authoritative is True


def test_authoritative_anchor_announces_a_real_move():
    c = PlaybackClock("sendspin")
    c.anchor(30.0, 100.0, rate=1.0, track_key="s1")
    c.take_discontinuity()
    c.anchor(90.0, 105.0, rate=1.0, track_key="s1")  # a seek
    assert c.take_discontinuity() is True
    assert c.position(105.0) == pytest.approx(90.0, abs=1e-9)


def test_authoritative_zero_rate_pauses():
    c = PlaybackClock("sendspin")
    c.anchor(30.0, 100.0, rate=1.0, track_key="s1")
    c.anchor(31.0, 101.0, rate=0.0, track_key="s1")
    assert c.playing is False
    assert c.position(200.0) == pytest.approx(31.0, abs=1e-9)


def test_authoritative_feed_mutes_the_ha_estimate():
    """Two feeds writing the same playhead would fight; the exact one wins."""
    c = PlaybackClock("sendspin")
    d = Driver(c)
    c.anchor(30.0, d.t, rate=1.0, track_key=d.track)
    d.run(2.0)
    exact = d.pos()
    # Home Assistant's coarser view of the same moment must be ignored...
    assert d.report(exact + 0.25) is False
    assert d.pos() == pytest.approx(exact, abs=1e-6)
    # ...until the exact feed goes away, at which point it takes over again.
    c.set_authoritative(False)
    d.run(1.0)
    assert d.report(d.pos() + 0.25) is True


def test_authoritative_feed_still_follows_a_pause():
    """Muting the estimate must not mute stop/pause, which HA still owns."""
    c = PlaybackClock("sendspin")
    d = Driver(c)
    c.anchor(30.0, d.t, rate=1.0, track_key=d.track)
    d.run(1.0)
    d.report(31.0, state="paused")
    assert c.playing is False
    frozen = d.pos()
    d.run(5.0)
    assert d.pos() == pytest.approx(frozen, abs=1e-9)


def test_authoritative_playback_speed_is_honoured():
    c = PlaybackClock("sendspin")
    c.anchor(10.0, 100.0, rate=1.0, track_key="s1")
    c.anchor(10.0, 100.0, rate=1.02, track_key="s2")
    assert c.rate == pytest.approx(1.02)
    assert c.position(110.0) == pytest.approx(10.0 + 10.0 * 1.02, abs=1e-9)


# -- the Sendspin server-clock filter -----------------------------------------


def _exchange(f: ClockFilter, client_t: float, offset: float, rtt: float,
              drift: float = 0.0, t0: float = 0.0) -> None:
    """One symmetric NTP exchange with a given true offset and round trip."""
    true_offset = offset + drift * (client_t - t0)
    f.update(
        client_transmitted=client_t,
        server_received=client_t + rtt / 2 + true_offset,
        server_transmitted=client_t + rtt / 2 + true_offset,
        client_received=client_t + rtt,
    )


def test_clock_filter_converges_on_a_constant_offset():
    f = ClockFilter()
    assert f.ready is False
    for i in range(20):
        _exchange(f, 100.0 + i * 5.0, offset=12.345, rtt=0.004)
    assert f.ready is True
    assert f.offset == pytest.approx(12.345, abs=1e-3)
    assert f.to_server(200.0) == pytest.approx(212.345, abs=1e-3)
    assert f.to_client(212.345) == pytest.approx(200.0, abs=1e-3)


def test_clock_filter_tracks_drift():
    """The drift term is what keeps the estimate good between slow exchanges."""
    f = ClockFilter()
    drift = 50e-6  # 50 ppm
    for i in range(40):
        _exchange(f, 100.0 + i * 5.0, offset=1.0, rtt=0.002, drift=drift, t0=100.0)
    last = 100.0 + 39 * 5.0
    expected = 1.0 + drift * (last - 100.0)
    assert f.offset == pytest.approx(expected, abs=2e-4)


def test_clock_filter_recovers_from_an_outlier():
    """A stalled exchange must not poison the estimate for long."""
    f = ClockFilter()
    for i in range(15):
        _exchange(f, 100.0 + i * 5.0, offset=2.0, rtt=0.003)
    good = f.offset
    # One exchange delayed asymmetrically by 200 ms.
    t = 100.0 + 15 * 5.0
    f.update(
        client_transmitted=t,
        server_received=t + 0.2 + 2.0,
        server_transmitted=t + 0.2 + 2.0,
        client_received=t + 0.21,
    )
    for i in range(16, 24):
        _exchange(f, 100.0 + i * 5.0, offset=2.0, rtt=0.003)
    assert f.offset == pytest.approx(good, abs=5e-3)


def test_clock_filter_round_trips():
    f = ClockFilter()
    for i in range(10):
        _exchange(f, 100.0 + i * 5.0, offset=-7.5, rtt=0.006)
    for t in (100.0, 150.0, 500.0):
        assert f.to_client(f.to_server(t)) == pytest.approx(t, abs=1e-6)
