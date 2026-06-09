"""3D spatial renderer: geometry, wave propagation, and that the predictive +
spatial pipeline still honours the non-bypassable flash limiter."""

from __future__ import annotations

import numpy as np
import pytest

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.structure import StructureTracker
from hue_music_sync.audio.tempo import TempoTracker
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.safety import MAX_FLASHES_PER_S, FieldSafety, _field_brightness
from hue_music_sync.effects.spatial import (
    Wave,
    floor_origin,
    height_band,
    normalize_positions,
)
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.025
_PERIOD = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE


# --- geometry ------------------------------------------------------------

def test_normalize_positions_uses_actual_extent():
    chans = [
        EntertainmentChannel(channel_id=0, x=-1.0, y=0.0, z=0.0),
        EntertainmentChannel(channel_id=1, x=1.0, y=0.0, z=2.0),
    ]
    pos = normalize_positions(chans)
    assert pos[0] == (0.0, 0.5, 0.0)  # y has no spread -> collapses to 0.5
    assert pos[1] == (1.0, 0.5, 1.0)


def test_floor_origin_is_central_and_low():
    pos = {0: (0.0, 0.0, 0.5), 1: (1.0, 1.0, 0.2), 2: (0.5, 0.5, 0.9)}
    ox, oy, oz = floor_origin(pos)
    assert ox == pytest.approx(0.5) and oy == pytest.approx(0.5)
    assert oz == pytest.approx(0.2)  # minimum height


def test_height_band_maps_low_to_bass_high_to_treble():
    assert height_band(0.0) == "sub_bass"
    assert height_band(0.99) == "high"


def test_wavefront_reaches_near_lamps_before_far_lamps():
    # The peak of a launched wave arrives at a near point earlier (younger age)
    # than at a far point — i.e. it propagates outward.
    wave = Wave(origin=(0.0, 0.0, 0.0), strength=1.0, speed=1.5, width=0.3)
    near, far = 0.3, 1.2

    def peak_age(d):
        best_age, best_amp = 0.0, -1.0
        w = Wave(origin=(0,0,0), strength=1.0, speed=1.5, width=0.3)
        for _ in range(200):
            amp = w.amplitude_at(d)
            if amp > best_amp:
                best_amp, best_age = amp, w.age
            w.advance(0.01, decay_tau=10.0)  # slow decay so the peak is clear
        return best_age

    assert peak_age(near) < peak_age(far)


# --- engine spatial behaviour -------------------------------------------

def _spread_channels(n=6):
    # Lamps spread across width and height so distance-from-origin varies.
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=(i % 3) / 2.0)
        for i in range(n)
    ]


def test_beat_produces_a_non_uniform_field():
    eng = EffectEngine(_spread_channels())
    eng.set_mode(SyncMode.INTENSE)
    beat = AnalysisFrame(
        bands={"sub_bass": 1.0, "bass": 1.0, "high": 0.4}, energy=0.8,
        beat=True, beat_strength=2.5,
    )
    quiet = AnalysisFrame(bands={"sub_bass": 0.1, "bass": 0.1, "high": 0.1}, energy=0.2)
    eng.render(beat, _DT)
    spreads = []
    for _ in range(8):  # let the wave travel across the room
        out = eng.render(quiet, _DT)
        vals = [max(c) for c in out.values()]
        spreads.append(np.std(vals))
    assert max(spreads) > 0.02  # lamps are not all doing the same thing


# --- the safety guarantee survives the new pipeline ----------------------

def _aggressive_stream(n):
    """Aggressive EDM-ish AnalysisFrames with timestamps and flux for the grid."""
    for i in range(n):
        beat = i % 8 == 0  # ~150 BPM at 50 fps
        lvl = 1.0 if beat else 0.15
        yield AnalysisFrame(
            bands={"sub_bass": lvl, "bass": lvl, "low_mid": lvl * 0.7, "mid": lvl * 0.5, "high": lvl * 0.6},
            energy=lvl, beat=beat, beat_strength=3.0 if beat else 0.0,
            flux=1.0 if beat else 0.05, t_audio=i * _PERIOD, centroid=0.4,
        )


@pytest.mark.parametrize(
    "mode", [SyncMode.SUBTLE, SyncMode.MEDIUM, SyncMode.HIGH, SyncMode.INTENSE]
)
def test_full_predictive_spatial_pipeline_stays_within_flash_limit(mode):
    eng = EffectEngine(_spread_channels())
    eng.set_mode(mode)
    tempo = TempoTracker(_PERIOD)
    structure = StructureTracker(_PERIOD)
    safety = FieldSafety()
    max_window = 0
    for frame in _aggressive_stream(900):
        grid = tempo.update(frame.t_audio, frame.flux, frame.beat, frame.beat_strength)
        st = structure.update(frame)
        out = eng.render(frame, _DT, grid, st)
        safety.process(out, _DT)
        max_window = max(max_window, len(safety._flashes))
    assert max_window <= MAX_FLASHES_PER_S
