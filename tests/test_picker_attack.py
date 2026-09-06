"""P5: the Auto picker's live estimator fix (from CAMusic) — pinned.

The live attack term must be sampled on frames that carry an onset, not
averaged over every frame. Averaging over every frame drags the heaviest
character weight toward ~0 (the width is near zero between transients — most
frames of any track), the earned band's ceiling collapses, and the picker sits
on the lowest enabled rung.
"""

from __future__ import annotations

import pytest

from hue_music_sync.const import DEFAULT_AUTO_LEVELS, SyncMode
from hue_music_sync.effects.modes import AutoIntensityPicker

DEFAULT = tuple(DEFAULT_AUTO_LEVELS)


def _drive(
    picker: AutoIntensityPicker,
    *,
    seconds: float,
    onset_every: float,
    onset_width: float,
    dt: float = 0.02,
) -> float:
    """Feed a steady, loud groove; return the picker's live attack estimate.

    Beats arrive every ``onset_every`` seconds; between them the frame carries
    no onset (onset_width ~0), exactly the distribution the old estimator
    averaged flat.
    """
    t = 0.0
    last = -1e9
    while t < seconds:
        beat = (t - last) >= onset_every
        if beat:
            last = t
        picker.update(
            dt,
            energy=0.8,
            salience=0.9,
            bpm=120.0,
            beat=beat,
            allowed=DEFAULT,
            onset_width=onset_width if beat else 0.0,
            flux=0.5,
        )
        t += dt
    return picker._char_attack


def test_attack_estimate_survives_frames_without_onsets():
    # Onsets every 0.5 s at width 0.25 (a solid groove's width): sampled on
    # onsets the estimate converges near the onset width; averaged over every
    # frame it collapsed to <0.05 and the band ceiling collapsed with it.
    picker = AutoIntensityPicker()
    attack = _drive(picker, seconds=40.0, onset_every=0.5, onset_width=0.25)
    assert attack > 0.15, f"attack collapsed to {attack} again"


def test_attack_estimate_tracks_what_the_onsets_measure():
    # The estimate should sit near the width the onsets actually carry, not
    # near (width * onset_rate * dt) — the old every-frame average.
    sparse = _drive(AutoIntensityPicker(), seconds=40.0, onset_every=1.0, onset_width=0.25)
    dense = _drive(AutoIntensityPicker(), seconds=40.0, onset_every=0.25, onset_width=0.25)
    # Both grooves carry the same per-onset width; both estimates should read
    # as a groove of that width (the OLD estimator made `sparse` ~4x quieter
    # than `dense` purely because fewer frames carried an onset).
    assert abs(dense - sparse) < 0.06, f"dense={dense} sparse={sparse}"
    assert dense > 0.15 and sparse > 0.15
