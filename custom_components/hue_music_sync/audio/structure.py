"""Musical structure: builds, drops and breakdowns.

What separates "reacts to volume" from "feels the music" is responding to the
*arc* of a track, not just the instantaneous level. This tracker watches slow
envelopes of loudness and spectral brightness (centroid) plus onset density and
classifies the moment:

* **building** — a riser/tension section: brightness and energy climbing, often
  with the bass thinning out and snare rolls accelerating. ``build_progress``
  ramps 0→1.
* **drop** — the release after a build: a sharp broadband energy surge (the bass
  slamming back in). Fires ``drop_now`` once, then holds a refractory.
* **breakdown** — energy collapses well below the running average.
* **steady** — everything else (verse/loop).

The engine uses this to tighten and desaturate through a build, then detonate a
full-field swell on the drop — always still bounded by the non-bypassable flash
limiter. It is purely reactive today; ``update`` takes an optional ``lookahead``
slice of future frames so a decode-ahead source can later make ``drop_imminent``
truly predictive (see the timing model). Heuristic prediction (a build that has
nearly maxed out) drives ``drop_imminent`` in the meantime.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

PHASE_STEADY = "steady"
PHASE_BUILDING = "building"
PHASE_DROP = "drop"
PHASE_BREAKDOWN = "breakdown"


@dataclass(slots=True)
class StructureState:
    phase: str = PHASE_STEADY
    build_progress: float = 0.0  # 0..1 tension through a build
    drop_now: bool = False  # the single frame a drop lands
    drop_imminent: bool = False  # a drop is expected very soon
    breakdown: bool = False


def _alpha(tau_s: float, dt: float) -> float:
    """EMA smoothing factor for a given time constant."""
    return 1.0 - pow(2.71828182845905, -dt / max(1e-6, tau_s))


class StructureTracker:
    def __init__(self, frame_period: float) -> None:
        self._dt = frame_period
        self._e_fast = 0.0
        self._e_slow = 0.0
        self._e_long = 0.0
        self._c_slow = 0.0
        win = max(8, int(round(2.5 / frame_period)))  # ~2.5 s trend window
        self._e_hist: deque[float] = deque(maxlen=win)
        self._c_hist: deque[float] = deque(maxlen=win)
        self._build = 0.0
        self._drop_refractory = 0.0
        self._started = False

    def update(
        self,
        frame,
        lookahead: Sequence = (),  # future frames, when a decode-ahead source exists
    ) -> StructureState:
        dt = self._dt
        energy = float(frame.energy)
        centroid = float(frame.centroid)
        bass = max(frame.bands.get("sub_bass", 0.0), frame.bands.get("bass", 0.0))

        if not self._started:
            self._e_fast = self._e_slow = self._e_long = energy
            self._c_slow = centroid
            self._started = True

        self._e_fast += _alpha(0.20, dt) * (energy - self._e_fast)
        self._e_slow += _alpha(1.5, dt) * (energy - self._e_slow)
        self._e_long += _alpha(6.0, dt) * (energy - self._e_long)
        self._c_slow += _alpha(1.5, dt) * (centroid - self._c_slow)

        e_trend = self._e_slow - (self._e_hist[0] if self._e_hist else self._e_slow)
        c_trend = self._c_slow - (self._c_hist[0] if self._c_hist else self._c_slow)
        self._e_hist.append(self._e_slow)
        self._c_hist.append(self._c_slow)

        if self._drop_refractory > 0.0:
            self._drop_refractory = max(0.0, self._drop_refractory - dt)

        # Build tension: brightness and energy trending up together.
        build_score = max(0.0, 2.2 * max(0.0, c_trend) + 1.6 * max(0.0, e_trend))
        build_score = min(1.0, build_score)
        # Ease build_progress up while tension persists, decay when it fades.
        rate = _alpha(0.6, dt) if build_score > self._build else _alpha(2.0, dt)
        self._build += rate * (build_score - self._build)

        # Drop: a sharp broadband surge after a build, with the bass slamming in.
        surge = self._e_fast - self._e_slow
        drop_now = False
        if (
            self._drop_refractory <= 0.0
            and self._build > 0.40
            and surge > 0.22
            and bass > 0.5
        ):
            drop_now = True
            self._drop_refractory = 1.5
            self._build = 0.0  # tension released

        breakdown = self._e_long > 0.15 and self._e_slow < 0.45 * self._e_long

        drop_imminent = self._build > 0.60 and self._drop_refractory <= 0.0
        # Optional: a decode-ahead source can confirm an imminent drop by spotting
        # the energy discontinuity in the upcoming frames.
        if lookahead:
            drop_imminent = drop_imminent or self._scan_lookahead(lookahead)

        if drop_now:
            phase = PHASE_DROP
        elif breakdown:
            phase = PHASE_BREAKDOWN
        elif self._build > 0.35:
            phase = PHASE_BUILDING
        else:
            phase = PHASE_STEADY

        return StructureState(
            phase=phase,
            build_progress=self._build,
            drop_now=drop_now,
            drop_imminent=drop_imminent,
            breakdown=breakdown,
        )

    def _scan_lookahead(self, lookahead: Sequence) -> bool:
        """True if the upcoming frames contain a clear energy discontinuity."""
        if len(lookahead) < 3:
            return False
        lo = min(f.energy for f in lookahead)
        hi = max(f.energy for f in lookahead)
        return (hi - lo) > 0.3 and lookahead[-1].energy > lookahead[0].energy

    def reset(self) -> None:
        self._started = False
        self._e_hist.clear()
        self._c_hist.clear()
        self._build = 0.0
        self._drop_refractory = 0.0
