"""Build a colour palette from a section's musical harmony (chroma).

The Hue+Spotify look reflects the song's *pitch and mood*, not just a static
theme. We do the same from our own offline analysis: a 12-bin chroma (pitch-
class energy) per section is mapped to colours - dominant pitch classes become
hue anchors, how tonal the section is sets saturation, and its timbral
brightness (spectral centroid) nudges warmth. The result is a :class:`Palette`
the engine samples exactly like album-art or preset palettes, so it rides the
same colour-jump / spread / flow pipeline.

Pure (numpy + colorsys), so the mapping is unit-tested without Home Assistant.
"""

from __future__ import annotations

import colorsys

import numpy as np

from .palette import RGB, Palette

# Pitch-class -> hue, the synesthetic chromatic rainbow (C=red, D~yellow, ...,
# B~violet) reported in pitch-class/colour studies. Index 0 = C, 11 = B. A
# plain chromatic wheel keeps adjacent semitones adjacent in hue, so a key and
# its neighbours give a coherent, smoothly-drifting palette.
PITCH_HUE: tuple[float, ...] = tuple(i / 12.0 for i in range(12))

# A chroma peak must reach this fraction of the strongest pitch class to earn
# its own colour anchor (keeps the palette to the section's real harmony).
_PEAK_FRAC = 0.45
_MAX_COLORS = 4


def palette_from_chroma(
    chroma: np.ndarray, centroid: float = 0.5, max_colors: int = _MAX_COLORS
) -> Palette | None:
    """Map a 12-bin chroma vector to a :class:`Palette`, or None if unusable.

    ``centroid`` (0..1 spectral brightness of the section) gently warms a dark/
    bassy section (more saturation) and airs out a bright one. Colours are
    ordered by hue so the cyclic gradient drifts smoothly.
    """
    c = np.asarray(chroma, dtype=np.float64)
    if c.size != 12 or not np.isfinite(c).all() or c.max() <= 1e-9:
        return None
    c = c / c.max()
    order = [int(i) for i in np.argsort(c)[::-1]]
    peaks = [p for p in order if c[p] >= _PEAK_FRAC][:max_colors]
    if len(peaks) < 2:  # always give the gradient at least two anchors
        peaks = order[:2]

    warm = 0.85 + 0.15 * (1.0 - max(0.0, min(1.0, centroid)))  # dark -> richer
    colors: list[RGB] = []
    for p in peaks:
        hue = PITCH_HUE[p]
        sat = max(0.0, min(1.0, (0.55 + 0.40 * float(c[p])) * warm))
        colors.append(colorsys.hsv_to_rgb(hue, sat, 1.0))
    colors.sort(key=lambda rgb: colorsys.rgb_to_hsv(*rgb)[0])
    return Palette(colors)


def dominant_pitch_class(chroma: np.ndarray) -> int:
    """The strongest pitch class (0=C .. 11=B), or -1 when chroma is empty."""
    c = np.asarray(chroma, dtype=np.float64)
    if c.size != 12 or c.max() <= 1e-9:
        return -1
    return int(np.argmax(c))
