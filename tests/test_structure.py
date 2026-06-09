"""Musical structure: builds, drops, breakdowns — and no false triggers."""

from __future__ import annotations

import random

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.structure import (
    PHASE_BUILDING,
    PHASE_DROP,
    StructureTracker,
)

_DT = 0.02


def _frame(energy, centroid, bass, beat=False, flux=0.0):
    return AnalysisFrame(
        bands={"sub_bass": bass, "bass": bass, "high": centroid},
        energy=energy,
        centroid=centroid,
        beat=beat,
        flux=flux,
    )


def test_build_then_drop():
    st = StructureTracker(_DT)
    saw_building = False
    build_peak = 0.0
    drops = 0
    # steady
    for i in range(int(3 / _DT)):
        st.update(_frame(0.4, 0.3, 0.6, beat=(i % 20 == 0)))
    # build: energy + centroid up, bass thinning
    n = int(4 / _DT)
    for i in range(n):
        p = i / n
        s = st.update(_frame(0.4 + 0.2 * p, 0.3 + 0.5 * p, 0.6 - 0.3 * p, beat=(i % 20 == 0)))
        saw_building = saw_building or s.phase == PHASE_BUILDING
        build_peak = max(build_peak, s.build_progress)
    # drop: bass slams back, broadband surge
    for i in range(int(0.3 / _DT)):
        s = st.update(_frame(0.95, 0.5, 0.95, beat=True))
        if s.drop_now:
            drops += 1
            assert s.phase == PHASE_DROP
    assert saw_building and build_peak > 0.4
    assert drops == 1  # exactly one drop edge, not a burst


def test_steady_music_never_false_triggers():
    st = StructureTracker(_DT)
    rng = random.Random(0)
    false_drops = 0
    max_build = 0.0
    for i in range(int(20 / _DT)):
        e = 0.5 + 0.08 * rng.uniform(-1, 1)
        b = 0.6 + 0.1 * rng.uniform(-1, 1)
        s = st.update(_frame(e, 0.35, b, beat=(i % 24 == 0)))
        false_drops += int(s.drop_now)
        max_build = max(max_build, s.build_progress)
    assert false_drops == 0
    assert max_build < 0.2  # steady loops don't read as a build


def test_lookahead_makes_drop_imminent_predictive():
    st = StructureTracker(_DT)
    # A rising energy ramp in the upcoming frames signals an imminent drop even
    # before the local build heuristic would.
    future = [_frame(0.2 + 0.1 * k, 0.3, 0.3) for k in range(6)]
    s = st.update(_frame(0.3, 0.3, 0.3), lookahead=future)
    assert s.drop_imminent
