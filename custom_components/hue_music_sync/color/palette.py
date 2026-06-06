"""Colour palettes and the built-in selectable schemes.

A :class:`Palette` is an ordered list of RGB anchor colours (0..1) that can be
sampled as a continuous cyclic gradient (``sample``) or spread across a fixed
number of lights (``spread``). The effects engine uses these to give each light
in an entertainment area a distinct but coherent colour.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

from ..const import ColorScheme

RGB = tuple[float, float, float]


@dataclass(slots=True)
class Palette:
    """An ordered set of RGB anchor colours, sampled as a cyclic gradient."""

    colors: list[RGB]

    def __post_init__(self) -> None:
        if not self.colors:
            self.colors = [(1.0, 1.0, 1.0)]

    def sample(self, pos: float) -> RGB:
        """Sample the gradient at ``pos`` (wraps cyclically)."""
        n = len(self.colors)
        if n == 1:
            return self.colors[0]
        pos = pos % 1.0
        scaled = pos * n
        i = int(scaled) % n
        j = (i + 1) % n
        frac = scaled - int(scaled)
        a, b = self.colors[i], self.colors[j]
        return (
            a[0] + (b[0] - a[0]) * frac,
            a[1] + (b[1] - a[1]) * frac,
            a[2] + (b[2] - a[2]) * frac,
        )

    def spread(self, count: int) -> list[RGB]:
        """Return ``count`` colours spread evenly across the gradient."""
        if count <= 0:
            return []
        if count == 1:
            return [self.colors[0]]
        return [self.sample(i / count) for i in range(count)]


def _hues(*hsv: tuple[float, float, float]) -> list[RGB]:
    return [colorsys.hsv_to_rgb(h, s, v) for h, s, v in hsv]


# Static schemes. Values chosen to look good on saturated Hue colour bulbs.
_SCHEMES: dict[ColorScheme, Palette] = {
    ColorScheme.WARM: Palette(_hues(
        (0.00, 1.0, 1.0), (0.04, 1.0, 1.0), (0.08, 0.9, 1.0), (0.95, 0.8, 1.0),
    )),
    ColorScheme.COOL: Palette(_hues(
        (0.55, 1.0, 1.0), (0.50, 0.9, 1.0), (0.70, 1.0, 1.0), (0.45, 0.8, 1.0),
    )),
    ColorScheme.NEON: Palette(_hues(
        (0.90, 1.0, 1.0), (0.50, 1.0, 1.0), (0.28, 1.0, 1.0), (0.78, 1.0, 1.0),
    )),
    ColorScheme.PARTY: Palette(_hues(
        (0.00, 1.0, 1.0), (0.13, 1.0, 1.0), (0.33, 1.0, 1.0),
        (0.50, 1.0, 1.0), (0.66, 1.0, 1.0), (0.83, 1.0, 1.0),
    )),
    ColorScheme.MONO: Palette(_hues(
        (0.08, 0.6, 1.0), (0.08, 0.3, 1.0), (0.08, 0.9, 0.8),
    )),
    ColorScheme.RAINBOW: Palette(_hues(*[(i / 12.0, 1.0, 1.0) for i in range(12)])),
}

# Fallback used when album-art extraction is unavailable.
ALBUM_ART_FALLBACK = ColorScheme.PARTY


def get_palette(scheme: ColorScheme) -> Palette:
    """Return the static palette for a scheme (ALBUM_ART falls back)."""
    if scheme == ColorScheme.ALBUM_ART:
        return _SCHEMES[ALBUM_ART_FALLBACK]
    return _SCHEMES[scheme]
