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


# Smooth, harmonious palettes — analogous hues that blend pleasantly as the
# colour drifts, rather than harsh primaries. Saturation is eased back from full
# so they're easy on the eyes while still vivid on Hue colour bulbs.
_SCHEMES: dict[ColorScheme, Palette] = {
    # Deep violet -> magenta -> coral -> orange -> warm gold.
    ColorScheme.SUNSET: Palette(_hues(
        (0.80, 0.80, 0.85), (0.92, 0.85, 1.0), (0.99, 0.85, 1.0),
        (0.045, 0.85, 1.0), (0.09, 0.80, 1.0), (0.12, 0.70, 1.0),
    )),
    # Aqua -> teal -> ocean blue -> deep blue.
    ColorScheme.OCEAN: Palette(_hues(
        (0.46, 0.75, 1.0), (0.50, 0.85, 1.0), (0.55, 0.90, 1.0),
        (0.60, 0.90, 1.0), (0.64, 0.85, 0.95),
    )),
    # Lime -> green -> emerald -> teal.
    ColorScheme.FOREST: Palette(_hues(
        (0.27, 0.70, 1.0), (0.33, 0.80, 1.0), (0.38, 0.85, 0.95),
        (0.44, 0.80, 0.95),
    )),
    # Soft lilac -> violet -> orchid -> rose.
    ColorScheme.LAVENDER: Palette(_hues(
        (0.72, 0.55, 1.0), (0.77, 0.65, 1.0), (0.83, 0.60, 1.0),
        (0.90, 0.55, 1.0),
    )),
    # Cosy red -> scarlet -> orange -> amber.
    ColorScheme.EMBER: Palette(_hues(
        (0.99, 0.90, 1.0), (0.02, 0.90, 1.0), (0.05, 0.90, 1.0),
        (0.09, 0.85, 1.0), (0.12, 0.80, 1.0),
    )),
    # Northern-lights teal -> green -> blue -> violet -> pink.
    ColorScheme.AURORA: Palette(_hues(
        (0.45, 0.80, 1.0), (0.36, 0.75, 1.0), (0.55, 0.80, 1.0),
        (0.72, 0.75, 1.0), (0.88, 0.65, 1.0),
    )),
    # Full spectrum: red -> orange -> yellow -> green -> cyan -> blue -> violet,
    # evenly around the hue wheel so the gradient (and the per-beat phase step)
    # sweeps cleanly through every colour.
    ColorScheme.RAINBOW: Palette(_hues(
        (0.00, 0.90, 1.0), (0.083, 0.90, 1.0), (0.167, 0.90, 1.0),
        (0.333, 0.85, 1.0), (0.500, 0.85, 1.0), (0.667, 0.85, 1.0),
        (0.833, 0.85, 1.0),
    )),
    # --- Philips Hue signature scenes ---
    # Tropical twilight: warm pink/purple sunset.
    ColorScheme.TROPICAL: Palette(_hues(
        (0.92, 0.80, 1.0), (0.85, 0.72, 1.0), (0.98, 0.80, 1.0),
        (0.04, 0.82, 1.0), (0.10, 0.70, 1.0),
    )),
    # Savanna sunset: golden ambers and soft reds.
    ColorScheme.SAVANNA: Palette(_hues(
        (0.01, 0.85, 1.0), (0.05, 0.85, 1.0), (0.08, 0.85, 1.0),
        (0.11, 0.80, 1.0), (0.13, 0.68, 1.0),
    )),
    # Spring blossom: soft pastels.
    ColorScheme.BLOSSOM: Palette(_hues(
        (0.95, 0.42, 1.0), (0.04, 0.38, 1.0), (0.13, 0.33, 1.0),
        (0.78, 0.34, 1.0), (0.55, 0.28, 1.0),
    )),
    # Honolulu: vibrant pink / orange / purple / teal.
    ColorScheme.HONOLULU: Palette(_hues(
        (0.95, 0.85, 1.0), (0.04, 0.85, 1.0), (0.80, 0.80, 1.0),
        (0.50, 0.80, 1.0),
    )),
    # Galaxy: deep blue / violet / magenta.
    ColorScheme.GALAXY: Palette(_hues(
        (0.66, 0.90, 1.0), (0.72, 0.85, 1.0), (0.78, 0.85, 1.0),
        (0.86, 0.78, 1.0),
    )),
}

# Fallback used when album-art extraction is unavailable.
ALBUM_ART_FALLBACK = ColorScheme.SUNSET


def get_palette(scheme: ColorScheme) -> Palette:
    """Return the static palette for a scheme.

    ALBUM_ART and SONG are dynamic (filled in at runtime from the cover art /
    the song's harmony), so they fall back to a pleasant static palette until
    their real colours are ready.
    """
    if scheme in (ColorScheme.ALBUM_ART, ColorScheme.SONG):
        return _SCHEMES[ALBUM_ART_FALLBACK]
    return _SCHEMES.get(scheme, _SCHEMES[ALBUM_ART_FALLBACK])
