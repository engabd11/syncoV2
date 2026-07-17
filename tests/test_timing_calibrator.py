"""Per-song timing calibrator: analyser-vs-audible gap -> a locked delay trim.

Pure logic (no Home Assistant), so the settling, robust estimation, locking and
clamping are unit-tested directly.
"""

from __future__ import annotations

from hue_music_sync.timing import TimingCalibrator

_DT = 0.02          # 50 fps
_LEAD = 0.15        # intended seek-ahead (s); the no-hang deviation is ~0


def _feed(cal, dev_ms, frames, *, jitter=None, beat_period_s=None, start_t=0.0):
    """Drive the calibrator with a synthetic stream whose true surplus-lead is
    ``dev_ms`` (optionally perturbed by ``jitter(i, dev_ms)``)."""
    for i in range(frames):
        t = start_t + i * _DT  # audible position advances in real time
        dev = jitter(i, dev_ms) if jitter else dev_ms
        analyzer = t + _LEAD + dev / 1000.0
        cal.update(
            _DT,
            analyzer_pos=analyzer,
            audible_pos=t,
            expected_lead_s=_LEAD,
            playing=True,
            beat_period_s=beat_period_s,
        )


def test_converges_and_locks_on_a_steady_offset():
    cal = TimingCalibrator()
    _feed(cal, 120, 260)  # > settle window
    assert cal.locked
    assert abs(cal.offset_ms - 120) <= 1


def test_zero_deviation_leaves_the_baseline_untouched():
    cal = TimingCalibrator()
    _feed(cal, 0, 260)
    assert cal.locked
    assert abs(cal.offset_ms) <= 1


def test_provisional_value_before_the_lock():
    cal = TimingCalibrator()
    _feed(cal, 90, 6)  # a handful of frames: past provisional, well before settle
    assert not cal.locked
    assert cal.offset_ms is not None
    assert abs(cal.offset_ms - 90) <= 1


def test_rejects_position_glitches():
    # Every 7th frame is a 1000 ms spike (a jumpy media_position); the estimate
    # must ignore them and settle on the true ~100 ms.
    def jitter(i, dev):
        return 1000.0 if i % 7 == 3 else dev

    cal = TimingCalibrator()
    _feed(cal, 100, 300, jitter=jitter)
    assert cal.locked
    assert abs(cal.offset_ms - 100) <= 10


def test_clamps_absurd_values():
    cal = TimingCalibrator()
    _feed(cal, 2000, 260)
    assert cal.offset_ms == 400  # _CLAMP_HI_MS


def test_holds_value_after_lock():
    cal = TimingCalibrator()
    _feed(cal, 100, 260)
    assert cal.locked
    locked_val = cal.offset_ms
    _feed(cal, 350, 260, start_t=100.0)  # a later, different gap
    assert cal.offset_ms == locked_val   # ignored once locked


def test_hard_lock_when_the_player_stays_noisy():
    # A wide, persistent spread (0..400 ms, within the outlier range) that never
    # satisfies the normal lock; the hard-lock timeout must still commit a value
    # rather than chase forever.
    def jitter(i, dev):
        return (i % 5) * 100.0

    cal = TimingCalibrator()
    _feed(cal, 0, 300, jitter=jitter)   # ~6 s: past settle, MAD ~100 ms
    assert not cal.locked               # the wide spread blocks the normal lock
    _feed(cal, 0, 200, jitter=jitter, start_t=6.0)  # cross the hard-lock timeout
    assert cal.locked


def test_no_value_without_position_data():
    cal = TimingCalibrator()
    for _ in range(50):
        cal.update(_DT, analyzer_pos=None, audible_pos=1.0,
                   expected_lead_s=_LEAD, playing=True)
        cal.update(_DT, analyzer_pos=1.0, audible_pos=None,
                   expected_lead_s=_LEAD, playing=True)
        cal.update(_DT, analyzer_pos=1.2, audible_pos=1.0,
                   expected_lead_s=_LEAD, playing=False)
    assert cal.offset_ms is None
    assert not cal.locked


def test_reset_forgets_the_track():
    cal = TimingCalibrator()
    _feed(cal, 120, 260)
    assert cal.locked
    cal.reset()
    assert cal.offset_ms is None
    assert not cal.locked
