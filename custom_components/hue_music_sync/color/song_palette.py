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


# Live-path palette throttling: the per-frame chroma is noisy, so the room's
# colour must only move on genuine harmonic shifts, never churn.
_HOLD_MIN_S = 3.0    # absolute minimum time between palette applications
_REFRESH_S = 8.0     # after this long, gentle drift may also re-anchor
_CHANGE_BIG = 0.30   # cosine distance that counts as a real harmonic shift
_CHANGE_SMALL = 0.10  # drift worth re-anchoring once _REFRESH_S has passed


def chroma_distance(a, b) -> float:
    """Cosine distance between two chroma vectors (0 = same key emphasis)."""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 1e-12 or nb <= 1e-12:
        return 1.0
    return 1.0 - float(np.dot(va, vb)) / (na * nb)


def should_update_palette(prev, cur, elapsed_s: float) -> bool:
    """Should the live Song palette be re-derived from ``cur`` now?

    ``prev`` is the chroma the current palette was built from (None = no
    palette applied yet — always apply). Within ``_HOLD_MIN_S`` nothing moves;
    a big harmonic shift applies immediately after that; small drift only
    re-anchors once ``_REFRESH_S`` has passed. An unchanged key never churns.
    """
    if prev is None:
        return True
    if elapsed_s < _HOLD_MIN_S:
        return False
    d = chroma_distance(prev, cur)
    if d > _CHANGE_BIG:
        return True
    return elapsed_s >= _REFRESH_S and d > _CHANGE_SMALL
