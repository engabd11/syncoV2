"""Extract a vivid colour palette from the current track's album art.

ffmpeg decodes the artwork (any URL it can fetch) to a small raw RGB thumbnail;
a light k-means then yields the dominant colours. Because brightness is driven by
the music, extracted colours are pushed to high saturation/value so even muted
covers map to lively bulb colours while keeping the cover's *hues*.
"""

from __future__ import annotations

import asyncio
import colorsys
import logging

import numpy as np

from .palette import RGB, Palette

_LOGGER = logging.getLogger(__name__)

_THUMB = 48  # decode artwork to THUMB x THUMB before clustering


def _kmeans_palette(pixels: np.ndarray, k: int = 5, iters: int = 12) -> list[RGB]:
    """Cluster RGB pixels (N,3 float 0..1) into up to ``k`` dominant colours."""
    if pixels.shape[0] == 0:
        return []
    k = min(k, pixels.shape[0])

    # Deterministic seeding: spread initial centroids along sorted luminance.
    lum = pixels @ np.array([0.299, 0.587, 0.114], dtype=pixels.dtype)
    order = np.argsort(lum)
    seed_idx = order[np.linspace(0, len(order) - 1, k).astype(int)]
    centroids = pixels[seed_idx].copy()

    labels = np.zeros(pixels.shape[0], dtype=np.int32)
    for _ in range(iters):
        dists = np.linalg.norm(pixels[:, None, :] - centroids[None, :, :], axis=2)
        labels = np.argmin(dists, axis=1)
        moved = False
        for c in range(k):
            members = pixels[labels == c]
            if members.shape[0]:
                new_c = members.mean(axis=0)
                if not np.allclose(new_c, centroids[c]):
                    moved = True
                centroids[c] = new_c
        if not moved:
            break

    counts = np.bincount(labels, minlength=k)
    ranked = np.argsort(counts)[::-1]

    palette: list[RGB] = []
    for c in ranked:
        if counts[c] == 0:
            continue
        r, g, b = (float(x) for x in centroids[c])
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        # Boost towards vivid bulb colours; brightness comes from the music.
        s = max(s, 0.55)
        v = 1.0
        palette.append(colorsys.hsv_to_rgb(h, s, v))
    return palette


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
