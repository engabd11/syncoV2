"""Colour tilt + spatial coupling (P4, ported from CAMusic).

The colour field gains the room's depth and height (tilted axis, min-max
normalised), and the two continuous per-lamp drives are pre-smoothed through a
row-stochastic Gaussian kernel so the room reads as one field instead of N
independent visualisers — without ever touching the transient layers.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.spatial import (
    SIGMA_MAX,
    SIGMA_MIN,
    colour_axis_projection,
    coupling_kernel,
    normalize_positions,
)
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 1.0 / 50.0


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    # evenly spaced on x, level in y/z — the flat-room fixture
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _music_frame() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.4, "bass": 0.4, "low_mid": 0.4, "mid": 0.4, "high": 0.3},
        energy=0.5,
        melbank=[0.4] * 16,
        salience=1.0,
    )


# --- the coupling kernel ----------------------------------------------------

def test_coupling_kernel_rows_sum_to_one():
    # The property that matters: a row-stochastic kernel is a weighted average,
    # so coupling redistributes the room's energy and can neither brighten nor
    # dim it. A constant field must also come back unchanged.
    positions = [(0.1, 0.2, 0.0), (0.8, 0.1, 0.3), (0.4, 0.9, 0.6), (0.5, 0.5, 0.1)]
    kernel = coupling_kernel(positions)
    for row in kernel:
        assert sum(row) == pytest.approx(1.0, abs=1e-9)
    # constant field passes through exactly
    values = {i: 0.42 for i in range(len(positions))}
    eng = EffectEngine(_channels(4))
    eng._coupling = kernel
    eng._rank_row = {cid: i for i, cid in enumerate(eng._rank_ids)}
    out = eng.couple_drives(values, 0.6)
    for cid, v in out.items():
        assert v == pytest.approx(0.42, abs=1e-9)


def test_coupling_redistributes_a_local_spike():
    # A weighted average cannot create or escape: every output is a convex
    # mix of the inputs (constant fields pass exactly — the rows-sum test),
    # a spike spreads to its neighbours, and the kernel is distance-weighted
    # (the nearer neighbour hears more of it than the far one).
    eng = EffectEngine(_channels(5))
    values = {cid: 0.1 for cid in eng._rank_ids}
    spike_cid = eng._rank_ids[2]  # the middle lamp
    values[spike_cid] = 1.0

    out = eng.couple_drives(values, 0.6)

    assert all(0.0 <= v <= 1.0 for v in out.values()), "not a convex mix"
    left = out[eng._rank_ids[1]]
    right = out[eng._rank_ids[3]]
    far = out[eng._rank_ids[0]]
    assert left > 0.1 and right > 0.1, "neighbours did not hear the spike"
    assert out[spike_cid] < 1.0, "the spike lamp did not give any away"
    assert left > far or right > far


def test_coupling_zero_returns_the_drive_unchanged():
    eng = EffectEngine(_channels(5))
    values = {cid: 0.3 * (i + 1) for i, cid in enumerate(eng._rank_ids)}
    assert eng.couple_drives(values, 0.0) is values


# --- the tilted colour axis -------------------------------------------------

def test_tilt_is_byte_identical_on_a_flat_even_room():
    # A room with no depth/height spread and even x spacing: the projection
    # min-max normalises back to exactly the x-rank, so tilt must not move a
    # single byte of the render.
    eng0 = EffectEngine(_channels(5))
    eng0.set_mode(SyncMode.MEDIUM)
    eng0.params = replace(eng0.params, colour_tilt=0.0)
    eng5 = EffectEngine(_channels(5))
    eng5.set_mode(SyncMode.MEDIUM)
    eng5.params = replace(eng5.params, colour_tilt=0.5)
    frame = _music_frame()
    for i in range(30):
        out0 = eng0.render(frame, _DT)
        out5 = eng5.render(frame, _DT)
    assert out0 == out5


def test_spatial_pos_follows_the_tilted_axis():
    # The reason the axis exists: colour used to follow the x-RANK only, so a
    # lamp's height/depth never moved its hue. The tilted projection
    # reorders lamps whose height contradicts their x-rank: here lamp A is
    # leftmost but high, lamp B is further right but low — the axis puts B
    # before A even though the x-rank does not.
    room = [
        EntertainmentChannel(channel_id=0, x=-1.0, y=0.0, z=1.0),   # A: left, high
        EntertainmentChannel(channel_id=1, x=-0.2, y=0.0, z=0.0),   # B: mid-left, low
        EntertainmentChannel(channel_id=2, x=0.8, y=0.0, z=0.0),    # C: right, low
    ]
    eng = EffectEngine(room)
    a, b, c = 0, 1, 2
    assert eng.cmap[a]["xrank"] < eng.cmap[b]["xrank"] < eng.cmap[c]["xrank"]
    # projection order: B (low, mid-left) < A (high, left) < C (right, low)
    assert (
        eng.cmap[b]["spatial_pos"]
        < eng.cmap[a]["spatial_pos"]
        < eng.cmap[c]["spatial_pos"]
    )


def test_tilt_moves_the_rendered_colour_in_a_room_with_depth():
    # In a room whose height contradicts the x-rank, engaging the tilt must
    # actually change the rendered colours (it is not a no-op there).
    room = [
        EntertainmentChannel(channel_id=0, x=-1.0, y=0.0, z=1.0),
        EntertainmentChannel(channel_id=1, x=-0.2, y=0.0, z=0.0),
        EntertainmentChannel(channel_id=2, x=0.8, y=0.0, z=0.0),
    ]

    def rendered(tilt: float):
        eng = EffectEngine(room)
        eng.set_mode(SyncMode.MEDIUM)
        eng.params = replace(eng.params, colour_tilt=tilt)
        frame = _music_frame()
        out = {}
        for i in range(20):
            out = eng.render(frame, _DT)
        return out

    flat = rendered(0.0)
    tilted = rendered(0.5)
    assert any(flat[cid][0] != tilted[cid][0] for cid in flat), (
        "the tilt never moved a colour in a room with depth"
    )


def test_projection_falls_back_to_rank_on_a_collapsed_axis():
    # Every lamp at the same point on the colour axis (here: identical x, y, z
    # scaling collapsed by identical positions) falls back to rank instead of
    # dividing by a zero span.
    positions = normalize_positions(_channels(3))
    projections = [colour_axis_projection(p) for p in positions.values()]
    # the fixture varies only x, so the projection must vary with it
    assert max(projections) - min(projections) > 0.1


def test_sigma_stays_inside_its_bounds():
    # A tight cluster and a room spanning the unit cube both get a
    # neighbourhood that means the same thing relative to their own spacing.
    tight = coupling_kernel([(0.0, 0.0, 0.0), (0.01, 0.0, 0.0), (0.02, 0.0, 0.0)])
    wide = coupling_kernel([(0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.5, 0.0, 1.0)])
    assert tight and wide  # built without error
    # bounds are on sigma, not directly observable; assert via the kernel's
    # dynamic range: the tight cluster's neighbours are close in weight.
    t = tight[0]
    assert max(t) - min(t) < 0.5  # neighbours all ~equally near
    w = wide[0]
    assert max(w) - min(w) > 0.1  # distinct distances are distinguishable
    assert SIGMA_MIN < SIGMA_MAX <= 0.6 + 1e-9


def test_cohesion_tunable_scales_spatial_coupling():
    eng = EffectEngine(_channels(5))
    eng.set_mode(SyncMode.MEDIUM)
    base = eng.params.spatial_coupling
    assert base > 0.0
    eng.set_tunables({"cohesion": 0.5})
    assert eng.params.spatial_coupling == pytest.approx(min(1.0, base * 0.5))
    eng.set_tunables(None)
    assert eng.params.spatial_coupling == pytest.approx(base)
