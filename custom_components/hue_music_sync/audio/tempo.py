"""Predictive beat grid: tempo + phase lock over the onset envelope.

The analyzer's spectral-flux onset detector tells us when a beat *just* happened
— but a great light show wants to know when the *next* beat will happen, so the
lights can start rising a hair early and **peak exactly on the kick** (cancelling
the bulb/Zigbee latency instead of chasing it). This is prediction from the
*rhythm model*, so it needs no look-ahead into future audio: once the tempo and
phase are locked, the next beat time is known.

Two stages, both cheap and numpy-only:

* **Tempo** — autocorrelation of the recent onset envelope, weighted by a
  log-Gaussian prior around ~120 BPM to suppress half/double-tempo octave errors
  (the Davies/Ellis approach).
* **Phase** — a phase-locked loop (PLL): a beat clock free-runs at the locked
  tempo and is nudged toward each detected onset, so it stays in the pocket and
  rides small tempo drift without jumping.

When the lock is weak (sparse/irregular onsets, speech, ambient) ``locked`` is
False and consumers fall back to plain reactive behaviour.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class BeatGrid:
    """The current rhythm model. Times are on the analyzer's ``t_audio`` clock."""

    bpm: float = 0.0
    confidence: float = 0.0  # 0..1 autocorrelation lock strength
    locked: bool = False
    period_s: float = 0.0  # seconds per beat (0 when unlocked)
    phase: float = 0.0  # position within the current beat, 0..1
    time_to_next_beat: float = 0.0  # seconds until the predicted next beat
    next_beat_t: float = 0.0  # absolute t_audio of the predicted next beat
    bar_phase: float = 0.0  # position within a 4-beat bar, 0..1
    predicted_beat: bool = False  # True on the frame the beat clock ticks over


_MIN_BPM = 70.0
_MAX_BPM = 190.0
_PRIOR_CENTER_BPM = 120.0
_PRIOR_WIDTH = 0.55  # log-tempo Gaussian width (octave ~= 0.69)
# Lock hysteresis: engage at the higher confidence, release at the lower one.
# A single threshold made the choreography flip between grid-paced and reactive
# behaviour mid-song whenever confidence hovered near it — which *looks* random.
# (Real music measures ~0.7-0.9; noise/irregular onsets stay well below.)
_LOCK_ON = 0.40
_LOCK_OFF = 0.25
# PLL discipline: only onsets near a predicted beat may nudge the clock; this
# many consecutive far-off onsets instead snap the phase to the onsets (the
# grid was simply anchored wrong, e.g. locked onto the off-beat).
_PLL_GATE = 0.20  # max |phase error| (in beats) for a nudge
_RESYNC_STREAK = 4


class TempoTracker:
    """Tracks tempo and beat phase from the per-frame onset (flux) stream."""

    def __init__(self, frame_period: float, history_s: float = 6.0) -> None:
        self._period = frame_period
        self._maxlen = max(64, int(history_s / frame_period))
        self._flux: deque[float] = deque(maxlen=self._maxlen)
        self._min_lag = max(1, int(round(60.0 / _MAX_BPM / frame_period)))
        self._max_lag = int(round(60.0 / _MIN_BPM / frame_period))
        # Precompute the log-tempo prior over candidate lags.
        lags = np.arange(self._min_lag, self._max_lag + 1)
        cand_bpm = 60.0 / (lags * frame_period)
        self._lags = lags
        self._prior = np.exp(
            -0.5 * (np.log(cand_bpm / _PRIOR_CENTER_BPM) / _PRIOR_WIDTH) ** 2
        ).astype(np.float64)

        self._bpm = 0.0
        self._period_s = 0.0
        self._confidence = 0.0
        self._locked = False
        self._offgrid_streak = 0
        self._phase = 0.0
        self._beat_count = 0
        self._frames = 0
        self._last_t: float | None = None
        self._recompute_every = max(1, int(round(0.10 / frame_period)))  # ~100 ms
        # Accumulate onset strength per beat-of-bar to find the downbeat.
        self._bar_accent = np.zeros(4, dtype=np.float64)
        self._downbeat = 0

    def update(
        self,
        t_audio: float,
        flux: float,
        beat: bool,
        beat_strength: float,
        bass: float = 1.0,
    ) -> BeatGrid:
        """Advance the grid by one frame and return the current prediction.

        ``bass`` (0..1, the frame's low-band level) weights how hard an onset
        may pull the clock: kicks steer, hi-hats barely do.
        """
        self._flux.append(max(0.0, flux))
        self._frames += 1
        if self._frames % self._recompute_every == 0 and len(self._flux) >= self._maxlen // 2:
            self._recompute_tempo()

        # Lock hysteresis (engage high, release low) so behaviour can't flap.
        if self._locked:
            if self._confidence < _LOCK_OFF or self._period_s <= 0.0:
                self._locked = False
        elif self._confidence >= _LOCK_ON and self._period_s > 0.0:
            self._locked = True
            self._offgrid_streak = 0

        # Advance the beat clock by the real elapsed audio time (frames may be
        # produced and consumed at slightly different rates).
        if self._last_t is None:
            dt = self._period
        else:
            dt = t_audio - self._last_t
            if dt <= 0.0 or dt > 0.25:  # gap/seek/reset: fall back to nominal
                dt = self._period
        self._last_t = t_audio

        predicted = False
        if self._period_s > 0.0:
            # Free-run the beat clock at the locked tempo.
            self._phase += dt / self._period_s
            if self._phase >= 1.0:
                self._phase -= 1.0
                self._beat_count += 1
                predicted = True
            # PLL: an onset *near a predicted beat* nudges the clock toward the
            # boundary; off-grid onsets (vocal hits, hi-hat flams) are ignored
            # — unless they form a streak, which means the grid is anchored on
            # the wrong phase and should snap to where the onsets actually are.
            if beat and self._locked:
                # Phase error in (-0.5, 0.5]: how far we are from the nearest beat.
                err = self._phase if self._phase <= 0.5 else self._phase - 1.0
                if abs(err) <= _PLL_GATE:
                    self._offgrid_streak = 0
                    gain = 0.10 * (0.4 + 0.6 * min(1.0, max(0.0, bass)))
                    self._phase -= gain * err
                    self._phase %= 1.0
                    self._accumulate_accent(beat_strength)
                else:
                    self._offgrid_streak += 1
                    if self._offgrid_streak >= _RESYNC_STREAK:
                        self._phase = 0.0  # re-anchor the clock on the onsets
                        self._offgrid_streak = 0

        locked = self._locked
        ttn = (1.0 - self._phase) * self._period_s if self._period_s > 0.0 else 0.0
        beat_idx = (self._beat_count - self._downbeat) % 4
        return BeatGrid(
            bpm=self._bpm,
            confidence=self._confidence,
            locked=locked,
            period_s=self._period_s,
            phase=self._phase,
            time_to_next_beat=ttn,
            next_beat_t=t_audio + ttn,
            bar_phase=(beat_idx + self._phase) / 4.0,
            predicted_beat=predicted,
        )

    def _recompute_tempo(self) -> None:
        env = np.fromiter(self._flux, dtype=np.float64)
        env = env - env.mean()
        if not np.any(env):
            self._confidence = 0.0
            return
        n = env.size
        ac = np.correlate(env, env, mode="full")[n - 1 :]
        zero = ac[0] if ac[0] > 1e-9 else 1e-9
        hi = min(self._max_lag, n - 1)
        if hi <= self._min_lag:
            return
        seg = ac[self._min_lag : hi + 1]
        prior = self._prior[: seg.size]
        scored = seg * prior
        best = int(np.argmax(scored))
        lag = self._lags[best]
        peak = float(seg[best] / zero)
        # Smooth the tempo so it drifts rather than jumps between recomputes.
        new_period = float(lag * self._period)
        if self._period_s <= 0.0:
            self._period_s = new_period
        else:
            self._period_s += 0.30 * (new_period - self._period_s)
        self._bpm = 60.0 / self._period_s
        self._confidence = max(0.0, min(1.0, peak))

    def _accumulate_accent(self, strength: float) -> None:
        idx = self._beat_count % 4
        self._bar_accent *= 0.97  # slowly forget old structure
        self._bar_accent[idx] += strength
        self._downbeat = int(np.argmax(self._bar_accent))

    def reset(self) -> None:
        self._flux.clear()
        self._bpm = 0.0
        self._period_s = 0.0
        self._confidence = 0.0
        self._locked = False
        self._offgrid_streak = 0
        self._phase = 0.0
        self._beat_count = 0
        self._bar_accent[:] = 0.0
        self._downbeat = 0
        self._last_t = None
