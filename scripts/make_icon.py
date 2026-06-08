"""Generate the Hue Music Sync brand icon (rainbow equaliser bars on a tile).

Writes the PNGs the home-assistant/brands repo expects:

    brands/custom_integrations/hue_music_sync/icon.png      256x256
    brands/custom_integrations/hue_music_sync/icon@2x.png   512x512

Run from the repo root:  ``python scripts/make_icon.py``  (requires Pillow).
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join("brands", "custom_integrations", "hue_music_sync")

BG = (24, 24, 38, 255)  # deep navy tile
BARS = [
    (255, 59, 48),   # red
    (255, 149, 0),   # orange
    (52, 199, 89),   # green
    (10, 132, 255),  # blue
    (191, 90, 242),  # violet
]
HEIGHTS = [0.46, 0.74, 1.00, 0.64, 0.40]  # fraction of inner height


def render(size: int) -> Image.Image:
    ss = 4  # supersample for clean antialiasing
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded tile filling the canvas.
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)

    # Equaliser bars, bottom-aligned, with rounded ends.
    n = len(BARS)
    side = s * 0.20
    gap = s * 0.045
    bar_w = (s - 2 * side - gap * (n - 1)) / n
    top_pad = s * 0.20
    bottom = s - s * 0.22
    inner_h = bottom - top_pad
    for i, (col, hf) in enumerate(zip(BARS, HEIGHTS)):
        x0 = side + i * (bar_w + gap)
        d.rounded_rectangle(
            [x0, bottom - inner_h * hf, x0 + bar_w, bottom],
            radius=bar_w / 2,
            fill=col + (255,),
        )

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for name, sz in [("icon.png", 256), ("icon@2x.png", 512)]:
        path = os.path.join(OUT, name)
        render(sz).save(path, optimize=True)
        print("wrote", path)


if __name__ == "__main__":
    main()
