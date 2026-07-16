"""Extract a *theme-faithful* colour palette from the current track's album art.

ffmpeg decodes the artwork to a small raw RGB thumbnail; the pixels are then
turned into a handful of lighting colours. The approach borrows from Android's
Palette / Vibrant.js, keeping **two swatch classes** so the palette captures the
album's mood, not just its brightest accent:

* **Perceptual clustering.** Pixels are clustered in CIELAB, where Euclidean
  distance matches perceived colour difference, so similar hues group and
  distinct ones separate (RGB clustering muddles both).
* **Accents** — vivid clusters, ranked by population × saturation so a small
  splash of vivid colour beats a large dull field.
* **Theme bases** — the muted / dark / metallic clusters that *are* the cover's
  atmosphere (a dark-silver-and-gold cover is mostly dark silver and gold).
  These keep their real saturation and **value**: a dark swatch renders as a
  dimmer light, and a near-neutral one becomes a warm- or cool-tinted white
  matched to its cast (silver → cool, gold → warm). Forcing everything vivid
  and full-bright (the old behaviour) destroyed exactly this.
* **Hue diversity + smooth ordering.** Accents are spread across the hue wheel
  and the final palette is ordered by hue, so colour drifts through related
  hues instead of jumping.
* **Graceful monochrome fallback.** A near-colourless cover yields its own
  dominant tint rather than an invented rainbow.

Colours may carry value < 1: the effect engine folds a swatch's value into the
light's brightness, so dark album themes give an authentically moodier show.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging

import numpy as np

from ..const import FFMPEG_PROTOCOL_ARGS
from ..util import redact_url
from .palette import RGB, Palette

_LOGGER = logging.getLogger(__name__)

_THUMB = 64  # decode artwork to THUMB x THUMB before clustering
_HUE_MIN_SEP = 0.055  # ~20 deg: reject near-duplicate accent hues in the palette
_SAT_FLOOR = 0.40  # fallback path only: floor for a rescued single dominant hue
# Swatch classification: vivid accents vs muted/dark theme bases.
_ACCENT_SAT = 0.35
_ACCENT_VAL = 0.35
_NEUTRAL_SAT = 0.12  # below this a swatch is a tinted white, not a colour
_BASE_MIN_POP = 0.10  # a base must really be part of the cover, not noise
_VALUE_FLOOR = 0.15  # keep even the darkest swatch faintly visible
# Tinted whites for near-neutral theme swatches, chosen by their colour cast.
_WARM_WHITE = (1.0, 0.84, 0.60)
_COOL_WHITE = (0.78, 0.86, 1.0)
_PLAIN_WHITE = (1.0, 0.92, 0.82)
# Soft warm white for covers with no real colour (black & white art) — nothing to
# be faithful to, so a neutral candle white reads better than an invented hue.
_NEUTRAL = (1.0, 0.86, 0.70)


def _rgb_to_hsv_components(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised value, saturation and luma for an (N,3) RGB array (0..1)."""
    mx = rgb.max(axis=1)
    mn = rgb.min(axis=1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=rgb.dtype)
    return mx, sat, luma


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Vectorised sRGB (0..1) -> CIELAB for perceptual clustering."""
    lin = np.where(rgb > 0.04045, ((rgb + 0.055) / 1.055) ** 2.4, rgb / 12.92)
    m = np.array(
        [[0.4124, 0.3576, 0.1805],
         [0.2126, 0.7152, 0.0722],
         [0.0193, 0.1192, 0.9505]],
        dtype=np.float64,
    )
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    L = 116.0 * f[:, 1] - 16.0
    a = 500.0 * (f[:, 0] - f[:, 1])
    b = 200.0 * (f[:, 1] - f[:, 2])
    return np.stack([L, a, b], axis=1)


def _kmeans(points: np.ndarray, k: int, iters: int = 14) -> np.ndarray:
    """Deterministic k-means; returns a label per point."""
    n = points.shape[0]
    k = min(k, n)
    # Seed centroids spread along lightness (first LAB axis) for stability.
    order = np.argsort(points[:, 0])
    seeds = order[np.linspace(0, n - 1, k).astype(int)]
    centroids = points[seeds].copy()
    labels = np.zeros(n, dtype=np.int32)
    for _ in range(iters):
        dists = np.linalg.norm(points[:, None, :] - centroids[None, :, :], axis=2)
        labels = np.argmin(dists, axis=1)
        moved = False
        for c in range(k):
            members = points[labels == c]
            if members.shape[0]:
                new_c = members.mean(axis=0)
                if not np.allclose(new_c, centroids[c]):
                    moved = True
                centroids[c] = new_c
        if not moved:
            break
    return labels


def _hue_distance(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def _low_colour_fallback(pixels: np.ndarray) -> list[RGB]:
    """Faithful fallback for covers with almost no colour.

    Returns the cover's single dominant *actual* hue (from whatever colourful
    pixels exist), or a neutral warm white for genuinely black-and-white art.
    Deliberately does NOT invent extra hues — a near-monochrome cover should
    drive a near-monochrome show, not a fabricated rainbow.
    """
    _, sat, _luma = _rgb_to_hsv_components(pixels)
    colourful = pixels[sat >= 0.12]
    if colourful.shape[0] >= 4:
        h, s, v = colorsys.rgb_to_hsv(*colourful.mean(axis=0))
        return [colorsys.hsv_to_rgb(h, max(s, _SAT_FLOOR), max(0.3, v))]
    return [_NEUTRAL]


def _tinted_white(mean_rgb: np.ndarray, value: float) -> RGB:
    """A near-neutral swatch as a white tinted by its own colour cast.

    Bulbs can't show "silver" or "charcoal", but a dim cool white reads as
    silver and a dim warm white as gold — keeping the value carries the mood.
    """
    r, _g, b = (float(x) for x in mean_rgb)
    if r - b > 0.02:
        tint = _WARM_WHITE
    elif b - r > 0.02:
        tint = _COOL_WHITE
    else:
        tint = _PLAIN_WHITE
    v = max(_VALUE_FLOOR, min(1.0, value))
    return (tint[0] * v, tint[1] * v, tint[2] * v)


def _kmeans_palette(pixels: np.ndarray, k: int = 5) -> list[RGB]:
    """Return up to ``k`` theme-faithful lighting colours from RGB pixels.

    ``pixels`` is an (N,3) float array in 0..1. Output colours keep their real
    saturation and value (the engine renders dark swatches as dimmer light).
    """
    if pixels.shape[0] == 0:
        return []
    _, _sat, luma = _rgb_to_hsv_components(pixels)
    # Drop only the true extremes (matte black frames, paper white); greys and
    # dark tones are kept — they are often the album's actual theme.
    body = pixels[(luma >= 0.04) & (luma <= 0.98)]
    if body.shape[0] < 6:
        return _low_colour_fallback(pixels)

    lab = _rgb_to_lab(body)
    n_clusters = min(14, body.shape[0], max(2 * k, 8))
    labels = _kmeans(lab, n_clusters)

    accents: list[tuple[float, float, float, float]] = []  # (score, h, s, v)
    bases: list[tuple[float, float, float, float, np.ndarray]] = []  # (pop, h, s, v, rgb)
    total = float(body.shape[0])
    for c in range(n_clusters):
        members = body[labels == c]
        if not members.shape[0]:
            continue
        mean = members.mean(axis=0)
        h, s, v = colorsys.rgb_to_hsv(*mean)
        pop = members.shape[0] / total
        if s >= _ACCENT_SAT and v >= _ACCENT_VAL:
            # Vividness-weighted population: a small vivid splash can outrank a
            # large dull field.
            accents.append((pop * (0.25 + 0.75 * s), h, s, v))
        else:
            bases.append((pop, h, s, v, mean))

    # Theme bases first: the dominant muted/dark swatches that set the mood.
    bases.sort(key=lambda t: -t[0])
    base_out: list[RGB] = []
    for pop, h, s, v, mean in bases:
        if len(base_out) >= 2 or pop < _BASE_MIN_POP:
            break
        if s < _NEUTRAL_SAT:
            base_out.append(_tinted_white(mean, v))
        else:
            base_out.append(colorsys.hsv_to_rgb(h, s, max(_VALUE_FLOOR, v)))

    # Vivid accents fill the remaining slots, hue-diverse.
    accents.sort(key=lambda t: -t[0])
    accent_out: list[RGB] = []
    picked_hues: list[float] = []
    for _score, h, s, v in accents:
        if len(base_out) + len(accent_out) >= k:
            break
        if all(_hue_distance(h, ph) >= _HUE_MIN_SEP for ph in picked_hues):
            picked_hues.append(h)
            accent_out.append(colorsys.hsv_to_rgb(h, s, max(_VALUE_FLOOR, v)))

    out = base_out + accent_out
    if not out:
        return _low_colour_fallback(pixels)
    # Return only the real colours found — never invent hues that aren't on the
    # cover. Order by hue so the cyclic gradient drifts smoothly between
    # related hues (tinted whites sort by their tint).
    out.sort(key=lambda c: colorsys.rgb_to_hsv(*c)[0])
    return out


async def extract_palette(ffmpeg_bin: str, url: str, k: int = 5) -> Palette | None:
    """Decode artwork at ``url`` with ffmpeg and return a vivid palette."""
    args = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error", *FFMPEG_PROTOCOL_ARGS, "-i", url,
        "-vf", f"scale={_THUMB}:{_THUMB}",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        raw, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, OSError) as exc:
        _LOGGER.debug("Album-art decode failed for %s: %s", redact_url(url), exc)
        return None

    expected = _THUMB * _THUMB * 3
    if len(raw) < expected:
        _LOGGER.debug(
            "Album-art decode short (%d/%d bytes): %s", len(raw), expected, redact_url(url)
        )
        return None

    pixels = (
        np.frombuffer(raw[:expected], dtype=np.uint8).astype(np.float32) / 255.0
    ).reshape(-1, 3)
    colors = await asyncio.get_running_loop().run_in_executor(
        None, _kmeans_palette, pixels, k
    )
    if not colors:
        return None
    return Palette(colors)
