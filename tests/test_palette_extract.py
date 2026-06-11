"""Theme-faithful palette extraction: dark/muted album themes survive."""

from __future__ import annotations

import colorsys

import numpy as np

from hue_music_sync.color.album_art import _kmeans_palette


def _hsv(c):
    return colorsys.rgb_to_hsv(*c)


def test_dark_purple_silver_gold_theme_survives():
    # A "Lumin Rain"-style cover: deep purple, dark silver, muted gold. The old
    # extractor forced everything vivid and full-bright, destroying the theme.
    purple = np.tile([0.35, 0.10, 0.50], (400, 1))   # vivid-ish, dark (v=0.5)
    silver = np.tile([0.72, 0.73, 0.78], (350, 1))   # near-neutral, cool cast
    gold = np.tile([0.45, 0.35, 0.12], (250, 1))     # muted dark gold
    palette = _kmeans_palette(np.vstack([purple, silver, gold]).astype(np.float32), k=5)
    assert palette

    hues = [_hsv(c) for c in palette]
    # The purple accent is present and keeps its *dark* value (not forced to 1).
    purples = [(h, s, v) for h, s, v in hues if 0.68 <= h <= 0.85 and s >= 0.4]
    assert purples, f"no purple in {hues}"
    assert all(v <= 0.75 for _h, _s, v in purples)  # stays deep, not neon
    # The silver appears as a dim cool-tinted white (low sat, value < 1).
    cools = [(h, s, v) for h, s, v in hues if s <= 0.30]
    assert cools, f"no neutral base kept in {hues}"
    assert all(v < 0.95 for _h, _s, v in cools)
    # The gold's warm hue family is represented.
    warms = [(h, s, v) for h, s, v in hues if (h <= 0.18 or h >= 0.95) and s >= 0.3]
    assert warms, f"no warm gold tone in {hues}"


def test_values_are_preserved_not_forced_to_full():
    # A uniformly dark vivid cover yields dark palette entries.
    deep_red = np.tile([0.40, 0.02, 0.05], (1000, 1)).astype(np.float32)
    palette = _kmeans_palette(deep_red, k=5)
    assert palette
    for c in palette:
        _h, s, v = _hsv(c)
        assert v <= 0.6  # the cover is dark; the palette stays dark
        assert s >= 0.5  # but keeps its real saturation


def test_vivid_covers_stay_vivid():
    # No bases on a bright vivid cover: behaviour matches the old extractor.
    red = np.tile([1.0, 0.05, 0.05], (300, 1))
    blue = np.tile([0.05, 0.05, 1.0], (300, 1))
    palette = _kmeans_palette(np.vstack([red, blue]).astype(np.float32), k=4)
    assert palette
    for c in palette:
        _h, s, v = _hsv(c)
        assert s >= 0.5 and v >= 0.9


def test_tiny_noise_clusters_do_not_become_bases():
    # 5% scattered dark pixels are noise, not a theme base (min population).
    vivid = np.tile([0.1, 0.7, 0.9], (950, 1))
    noise = np.random.RandomState(1).uniform(0.05, 0.25, (50, 3))
    palette = _kmeans_palette(np.vstack([vivid, noise]).astype(np.float32), k=5)
    assert palette
    # All output is the vivid cyan family; no random dark mud.
    for c in palette:
        h, s, _v = _hsv(c)
        assert abs(h - 0.53) < 0.1 and s >= 0.4
