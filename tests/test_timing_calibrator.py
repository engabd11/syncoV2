"""Per-song timing calibrator: analyser-vs-audible gap -> a delay correction.

Pure logic (no Home Assistant), so the settling, robust estimation, hysteresis
and clamping are unit-tested directly.

The contract that matters: the calibrator is fed **one observation per player
report** and ticked separately with wall time. Feeding it once per render frame
(50 fps) turns a single measurement into fifty identical ones, which yields a
tight, confident spread around a number that was never corroborated.
"""

from __future__ import annotations

from hue_music_sync.timing import TimingCalibrator, slew_toward

_DT = 0.02          # 50 fps render period
_REPORT_S = 1.0     # a player that reports its position about once a second


def _feed(cal, dev_ms, reports, *, jitter=None, beat_period_s=None, every=_REPORT_S):
    """Run ``reports`` player reports of a true surplus lead of ``dev_ms``.

    Wall time advances between reports at the render rate, exactly as the
    coordinator drives it: many ticks, one observation.
    """
    frames = max(1, int(round(every / _DT)))
    for i in range(reports):
        for _ in range(frames):
            cal.tick(_DT)
        dev = jitter(i, dev_ms) if jitter else dev_ms
        cal.observe(dev, beat_period_s=beat_period_s)


def test_converges_and_settles_on_a_steady_offset():
    cal = TimingCalibrator()
    _feed(cal, 120, 12)
    assert cal.settled
    assert abs(cal.offset_ms - 120) <= 1


def test_zero_deviation_leaves_the_baseline_untouched():
    cal = TimingCalibrator()
    _feed(cal, 0, 12)
    assert cal.settled
    assert abs(cal.offset_ms) <= 1


def test_provisional_value_before_settling():
    cal = TimingCalibrator()
    _feed(cal, 90, 2)  # past the provisional threshold, well short of settling
    assert not cal.settled
    assert cal.offset_ms is not None
    assert abs(cal.offset_ms - 90) <= 1


def test_one_sample_per_report_not_per_frame():
    """The defect this contract exists to prevent.

    Ticking for four seconds with three reports must count three samples — not
    two hundred — and must not present the result as settled.
    """
    cal = TimingCalibrator()
    for _ in range(200):
        cal.tick(_DT)
    for _ in range(3):
        cal.observe(120.0)
    assert cal.samples == 3
    assert not cal.settled


def test_settle_clock_uses_wall_dt():
    """100 render frames of 20 ms are 2.0 s of playback, not 1.67 s."""
    cal = TimingCalibrator()
    cal.observe(50.0)
    for _ in range(100):
        cal.tick(_DT)
    cal.observe(50.0)
    cal.observe(50.0)
    cal.observe(50.0)
    # Four observations spanning >= _MIN_SPAN_S: enough to judge the spread.
    assert cal.settled


def test_span_gate_rejects_a_burst():
    """Eight reports inside a fraction of a second prove nothing about spread."""
    cal = TimingCalibrator()
    for _ in range(8):
        cal.tick(_DT)
        cal.observe(75.0)
    assert not cal.settled


def test_rejects_position_glitches():
    """Every fourth report is a 1000 ms spike; the estimate must ignore them."""
    def jitter(i, dev):
        return 1000.0 if i % 4 == 3 else dev

    cal = TimingCalibrator()
    _feed(cal, 100, 24, jitter=jitter)
    assert abs(cal.offset_ms - 100) <= 10


def test_clamps_absurd_values():
    cal = TimingCalibrator()
    _feed(cal, 2000, 12)
    assert cal.offset_ms == 400  # the default upper clamp


def test_clamp_is_injectable():
    """Advancing is only possible within the baseline delay, which varies."""
    cal = TimingCalibrator()
    cal.set_clamp(-50.0, 200.0)
    _feed(cal, -500, 12)
    assert cal.offset_ms == -50
    cal2 = TimingCalibrator()
    cal2.set_clamp(-50.0, 200.0)
    _feed(cal2, 900, 12)
    assert cal2.offset_ms == 200


def test_set_clamp_reins_in_an_existing_value():
    cal = TimingCalibrator()
    _feed(cal, 380, 12)
    assert cal.offset_ms == 380
    cal.set_clamp(-200.0, 250.0)
    assert cal.offset_ms == 250


def test_holds_value_within_the_hysteresis_band():
    """A small wander must not twitch the show on every report."""
    def jitter(i, dev):
        return dev + (10.0 if i % 2 else -10.0)

    cal = TimingCalibrator()
    _feed(cal, 100, 12)
    committed = cal.offset_ms
    _feed(cal, 100, 12, jitter=jitter)
    assert cal.offset_ms == committed


def test_recommits_on_a_persistent_drift():
    """Nothing locks forever: a real, sustained shift must be adopted.

    This is what makes auto timing survive a mid-song player glitch instead of
    holding a stale value for the rest of the track.
    """
    cal = TimingCalibrator()
    _feed(cal, 100, 12)
    assert abs(cal.offset_ms - 100) <= 2
    _feed(cal, 250, 30)
    assert abs(cal.offset_ms - 250) <= 5


def test_noisy_player_still_commits_a_stable_value():
    """A wide spread yields a value that is honest about not being settled."""
    def jitter(i, dev):
        return (i % 5) * 100.0

    cal = TimingCalibrator()
    _feed(cal, 0, 30, jitter=jitter)
    assert cal.offset_ms is not None
    assert not cal.settled  # the spread is real; don't claim otherwise
    before = cal.offset_ms
    _feed(cal, 0, 30, jitter=jitter)
    assert abs(cal.offset_ms - before) <= 100


def test_beat_period_widens_the_gate():
    """On a slow track, a spread that is a small fraction of a beat is fine."""
    def jitter(i, dev):
        return dev + (40.0 if i % 2 else -40.0)

    fixed = TimingCalibrator()
    _feed(fixed, 100, 12, jitter=jitter)
    assert not fixed.settled
    musical = TimingCalibrator()
    _feed(musical, 100, 12, jitter=jitter, beat_period_s=1.0)  # gate 150 ms
    assert musical.settled


def test_no_value_without_observations():
    cal = TimingCalibrator()
    for _ in range(300):
        cal.tick(_DT)
    assert cal.offset_ms is None
    assert not cal.settled


def test_reset_forgets_the_track():
    cal = TimingCalibrator()
    _feed(cal, 120, 12)
    assert cal.settled
    cal.reset()
    assert cal.offset_ms is None
    assert not cal.settled
    assert cal.samples == 0


def test_locked_is_an_alias_for_settled():
    """The card reads `timing_locked`; the key survives the rename."""
    cal = TimingCalibrator()
    assert cal.locked is cal.settled
    _feed(cal, 40, 12)
    assert cal.locked is cal.settled is True


# -- the applied-delay slew limiter -------------------------------------------


def test_slew_adopts_immediately_from_none():
    """A genuine discontinuity has nothing buffered to protect."""
    assert slew_toward(None, 250.0, 0.02, 120.0) == 250.0


def test_slew_is_rate_limited_in_both_directions():
    assert slew_toward(0.0, 1000.0, 0.1, 120.0) == 12.0
    assert slew_toward(0.0, -1000.0, 0.1, 120.0) == -12.0


def test_slew_does_not_overshoot():
    assert slew_toward(100.0, 101.0, 1.0, 120.0) == 101.0


def test_slew_clamps_a_long_stall():
    """A frozen loop must not authorise one huge step in the applied delay."""
    assert slew_toward(0.0, 1000.0, 30.0, 120.0) == 30.0  # 0.25 s * 120


def test_slew_reaches_a_200ms_correction_within_two_seconds():
    """The number the design rests on: invisible, but not slow."""
    v = 0.0
    for _ in range(100):  # 2 s at 50 fps
        v = slew_toward(v, 200.0, 0.02, 120.0)
    assert v == 200.0
