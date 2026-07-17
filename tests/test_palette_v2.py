"""Album colours v2: population-weighted palettes.

Each cover colour carries its real share of the artwork, and the weighted
palette holds each colour for a slice of the cycle proportional to that
share — a 90% green / 10% red sleeve renders a green room that shifts to
red in moments, not a 50/50 gradient.
"""

from __future__ import annotations

import colorsys

import numpy as np

from hue_music_sync.color.album_art import _kmeans_palette_v2
from hue_music_sync.color.palette import Palette


def _hue(c) -> float:
    return colorsys.rgb_to_hsv(*c)[0]


def _dwell(palette: Palette, samples: int = 4000) -> dict[int, float]:
    """Fraction of the cycle spent nearest each anchor colour."""
    counts = {i: 0 for i in range(len(palette.colors))}
    for k in range(samples):
        c = palette.sample(k / samples)
        best = min(
            counts,
            key=lambda i: sum((a - b) ** 2 for a, b in zip(c, palette.colors[i])),
        )
        counts[best] += 1
    return {i: n / samples for i, n in counts.items()}


# --- weighted sampling ---------------------------------------------------------


def test_weighted_sample_dwell_matches_weights():
    green = (0.1, 0.9, 0.1)
    red = (0.9, 0.1, 0.1)
    pal = Palette([green, red], weights=[0.9, 0.1])
    dwell = _dwell(pal)
    assert abs(dwell[0] - 0.9) < 0.06  # ~90% of the cycle reads green
    assert dwell[1] > 0.04  # and red genuinely appears


def test_weighted_sample_is_continuous_at_boundaries():
    pal = Palette([(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)], weights=[0.7, 0.3])
    prev = pal.sample(0.0)
    for k in range(1, 1001):
        cur = pal.sample(k / 1000)
        assert max(abs(a - b) for a, b in zip(cur, prev)) < 0.06  # no hue pops
        prev = cur


def test_uniform_palette_unchanged_without_weights():
    colors = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    assert Palette(colors).sample(0.5) == Palette(list(colors)).sample(0.5)
    # Degenerate weights collapse safely back to the uniform gradient.
    assert Palette(list(colors), weights=[0.0, 0.0, 0.0]).weights is None
    assert Palette([(1.0, 1.0, 1.0)], weights=[1.0]).weights is None


def test_weights_normalised_and_padded():
    pal = Palette([(1, 0, 0), (0, 1, 0)], weights=[3.0])
    assert pal.weights is None or abs(sum(pal.weights) - 1.0) < 1e-9


# --- v2 extraction -------------------------------------------------------------


def test_ninety_ten_cover_extracts_matching_weights():
    green = np.tile([0.10, 0.85, 0.15], (900, 1))
    red = np.tile([0.85, 0.10, 0.10], (100, 1))
    colors, weights = _kmeans_palette_v2(
        np.vstack([green, red]).astype(np.float32), k=4
    )
    assert len(colors) == 2
    assert abs(sum(weights) - 1.0) < 1e-6
    by_hue = sorted(zip(colors, weights), key=lambda cw: -cw[1])
    (big, big_w), (small, small_w) = by_hue
    assert 0.20 <= _hue(big) <= 0.45  # green dominates
    assert big_w > 0.85
    assert small_w < 0.15


def test_caps_at_four_and_weights_cover_whole_image():
    rng = np.random.default_rng(7)
    hues = [0.0, 0.15, 0.35, 0.55, 0.75, 0.9]
    blocks = []
    for i, h in enumerate(hues):
        rgb = colorsys.hsv_to_rgb(h, 0.85, 0.9)
        blocks.append(np.tile(rgb, (150 + 30 * i, 1)))
    pixels = (np.vstack(blocks) + rng.normal(0, 0.01, (sum(b.shape[0] for b in blocks), 3))).clip(0, 1)
    colors, weights = _kmeans_palette_v2(pixels.astype(np.float32), k=4)
    assert len(colors) <= 4
    # Folded remainders keep the weights describing the WHOLE cover.
    assert abs(sum(weights) - 1.0) < 1e-6


def test_similar_hues_merge_and_pool_share():
    g1 = np.tile([0.10, 0.80, 0.15], (450, 1))
    g2 = np.tile([0.14, 0.86, 0.18], (450, 1))  # near-identical green
    red = np.tile([0.85, 0.10, 0.10], (100, 1))
    colors, weights = _kmeans_palette_v2(
        np.vstack([g1, g2, red]).astype(np.float32), k=4
    )
    assert len(colors) == 2  # the greens merged
    assert max(weights) > 0.8  # and pooled their share


def test_monochrome_cover_yields_single_full_weight():
    grey = np.tile([0.5, 0.5, 0.5], (500, 1)).astype(np.float32)
    colors, weights = _kmeans_palette_v2(grey, k=4)
    assert colors
    assert weights[0] > 0.9 or len(colors) == 1
