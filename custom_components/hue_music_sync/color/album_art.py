"""Extract a vivid, coherent colour palette from the current track's album art.

ffmpeg decodes the artwork to a small raw RGB thumbnail; the pixels are then
turned into a handful of lighting colours. The approach borrows from Android's
Palette / Vibrant.js but is tuned for *lights* rather than UI swatches:

* **Perceptual clustering.** Pixels are clustered in CIELAB, where Euclidean
  distance matches perceived colour difference, so similar hues group and
  distinct ones separate (RGB clustering muddles both).
* **Background rejection.** Near-white, near-black and near-grey pixels (the
  usual cover background / matte) are dropped before clustering, so the palette
  comes from the *subject*, not the wall behind it.
* **Vividness-weighted ranking.** Clusters are scored by population *and*
  saturation, so a small splash of vivid colour can beat a large dull field —
  the opposite of pure population ranking, which always picks the background.
* **Hue diversity + smooth ordering.** The chosen colours are spread across the
  hue wheel (no near-duplicates) and ordered by hue, so the palette drifts and
  the per-beat colour steps move through *related* hues instead of jumping.
* **Graceful monochrome fallback.** A genuinely single-colour cover yields a
  tasteful analogous spread around its dominant hue instead of one flat colour.

Brightness is driven by the music, so the chromaticity is what matters here:
colours are emitted at full value with a saturation floor so even muted covers
read as lively on the bulbs, while keeping the cover's actual hues.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging

import numpy as np

from .palette import RGB, Palette

_LOGGER = logging.getLogger(__name__)

_THUMB = 64  # decode artwork to THUMB x THUMB before clustering
_SAT_FLOOR = 0.5  # minimum output saturation so bulbs stay vivid
_HUE_MIN_SEP = 0.055  # ~20 deg: reject near-duplicate hues in the palette


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


def _analogous_spread(h: float, s: float, n: int) -> list[RGB]:
    """A tasteful analogous spread around a single dominant hue.

    Used when a cover is effectively one colour (greyscale, or a vivid but
    single-hue cover) so the lights still have related hues to drift through
    instead of sitting on one flat colour.
    """
    s = max(s, _SAT_FLOOR)
    out: list[RGB] = []
    for i in range(max(1, n)):
        hh = (h + (i - (n - 1) / 2.0) * 0.05) % 1.0  # +/- a few degrees
        out.append(colorsys.hsv_to_rgb(hh, s, 1.0))
    return out


def _monochrome_fallback(rgb: np.ndarray, n: int) -> list[RGB]:
    """Analogous spread around the cover's dominant hue (greyscale covers)."""
    h, s, _ = colorsys.rgb_to_hsv(*rgb.mean(axis=0))
    return _analogous_spread(h, s, n)


def _kmeans_palette(pixels: np.ndarray, k: int = 5) -> list[RGB]:
    """Return up to ``k`` vivid, hue-diverse lighting colours from RGB pixels.

    ``pixels`` is an (N,3) float array in 0..1. ``k`` is the desired number of
    output colours.
    """
    if pixels.shape[0] == 0:
        return []
    _, sat, luma = _rgb_to_hsv_components(pixels)
    keep = (sat >= 0.12) & (luma >= 0.06) & (luma <= 0.97)
    vivid = pixels[keep]
    # If almost nothing colourful survives, the cover is effectively monochrome.
    if vivid.shape[0] < max(8, int(0.04 * pixels.shape[0])):
        return _monochrome_fallback(pixels, k)

    lab = _rgb_to_lab(vivid)
    n_clusters = min(12, vivid.shape[0], max(2 * k, 8))
    labels = _kmeans(lab, n_clusters)

    clusters: list[tuple[float, float, float, float]] = []  # (score, h, s, v)
    total = float(vivid.shape[0])
    for c in range(n_clusters):
        members = vivid[labels == c]
        if not members.shape[0]:
            continue
        h, s, v = colorsys.rgb_to_hsv(*members.mean(axis=0))
        pop = members.shape[0] / total
        # Vividness-weighted population: a small vivid splash can outrank a large
        # dull field (pure population ranking would always pick the background).
        score = pop * (0.25 + 0.75 * s)
        clusters.append((score, h, s, v))

    clusters.sort(key=lambda t: -t[0])
    # Greedily pick the top scorers, rejecting near-duplicate hues for variety.
    picked: list[tuple[float, float, float]] = []  # (h, s, v)
    for _score, h, s, v in clusters:
        if len(picked) >= k:
            break
        if all(_hue_distance(h, ph) >= _HUE_MIN_SEP for ph, _, _ in picked):
            picked.append((h, s, v))
    if not picked:
        picked = [(clusters[0][1], clusters[0][2], clusters[0][3])]

    # A near-single-hue cover (vivid but one colour) reads better as an analogous
    # spread than one flat colour, so the lights have related hues to drift over.
    if len(picked) <= 1:
        h, s, _v = picked[0]
        return _analogous_spread(h, s, k)

    # Order by hue so the cyclic gradient drifts smoothly between related hues.
    picked.sort(key=lambda t: t[0])
    return [colorsys.hsv_to_rgb(h, max(s, _SAT_FLOOR), 1.0) for h, s, _v in picked]


async def extract_palette(ffmpeg_bin: str, url: str, k: int = 5) -> Palette | None:
    """Decode artwork at ``url`` with ffmpeg and return a vivid palette."""
    args = [
        ffmpeg_bin, "-nostdin", "-loglevel", "error", "-i", url,
        "-vf", f"scale={_THUMB}:{_THUMB}",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        raw, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, OSError) as exc:
        _LOGGER.debug("Album-art decode failed for %s: %s", url, exc)
        return None

    expected = _THUMB * _THUMB * 3
    if len(raw) < expected:
        _LOGGER.debug("Album-art decode short (%d/%d bytes): %s", len(raw), expected, url)
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
