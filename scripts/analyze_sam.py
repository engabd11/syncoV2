#!/usr/bin/env python3
"""Reverse-engineer Samsung music-sync behaviour from a recorded clip.

Extracts a per-frame average brightness/colour timeline (ffmpeg scale=1:1) and
the audio beat grid (our Analyzer), then reports how the lights behave relative
to the beats: how dark between beats, how bright on them, which beats trigger,
and the colour character.

    python analyze_sam.py "<video>" [start_seconds]
"""

from __future__ import annotations

import colorsys
import enum
import os
import subprocess
import sys
import types

import numpy as np

if not hasattr(enum, "StrEnum"):
    class _S(str, enum.Enum):
        def __str__(self): return self.value
    enum.StrEnum = _S  # type: ignore[attr-defined]
_CC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components")
if "hue_music_sync" not in sys.modules:
    _pkg = types.ModuleType("hue_music_sync"); _pkg.__path__ = [os.path.join(_CC, "hue_music_sync")]
    sys.modules["hue_music_sync"] = _pkg
from hue_music_sync.audio.analyzer import Analyzer  # noqa: E402
from hue_music_sync.const import ANALYSIS_HOP as HOP, ANALYSIS_SAMPLE_RATE as SR  # noqa: E402

VFPS = 30


def light_timeline(video: str, ss: str):
    """Per frame: whole-frame average (luma, RGB) — proxy for total light out."""
    p = subprocess.run(
        ["ffmpeg", "-ss", ss, "-i", video, "-vf", f"scale=1:1,fps={VFPS}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True,
    )
    rgb = np.frombuffer(p.stdout, dtype=np.uint8).reshape(-1, 3).astype(np.float32) / 255.0
    luma = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    return luma, rgb


def beat_grid(video: str, ss: str):
    p = subprocess.Popen(
        ["ffmpeg", "-ss", ss, "-i", video, "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    a = Analyzer()
    beats = []
    idx = 0
    hop_bytes = HOP * 2
    while True:
        raw = p.stdout.read(hop_bytes)
        if len(raw) < hop_bytes:
            break
        hop = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        f = a.push(hop)
        if f.beat:
            beats.append((idx * HOP / SR, f.beat_strength))
        idx += 1
    p.kill()
    return beats


def main():
    video = sys.argv[1]
    ss = sys.argv[2] if len(sys.argv) > 2 else "4"
    luma, rgb = light_timeline(video, ss)
    secs = len(luma) / VFPS
    norm = luma / max(luma.max(), 1e-6)
    beats = beat_grid(video, ss)
    print(f"=== {os.path.basename(video)} ===")
    print(f"frames: {len(luma)} ({secs:.1f}s)  onsets: {len(beats)} (~{len(beats)/secs*60:.0f}/min)")

    pct = {p: round(float(np.percentile(norm, p)), 2) for p in (10, 50, 90, 99)}
    print(f"whole-frame brightness: dark(10th)={pct[10]} median={pct[50]} bright(90th)={pct[90]} peak(99th)={pct[99]}")
    print(f"  contrast peak/median = {pct[99]/max(pct[50],0.01):.1f}x")

    # Flashes = brightness jumps above a 1s rolling baseline.
    base = np.convolve(norm, np.ones(VFPS) / VFPS, mode="same")
    rise = norm - base
    flashes = []
    for i in range(2, len(norm) - 1):
        if rise[i] > 0.08 and norm[i] >= norm[i - 1] and norm[i] >= norm[i + 1] and norm[i] > norm[i - 2]:
            if not flashes or (i - flashes[-1]) > VFPS * 0.2:
                flashes.append(i)
    print(f"flashes (rises above rolling baseline): {len(flashes)} (~{len(flashes)/secs*60:.0f}/min)  "
          f"= {len(flashes)/max(1,len(beats)):.2f} per onset")

    buckets = np.array_split(norm, 110)
    spark = "".join("#" if b.max() > 0.75 else "+" if b.max() > 0.5 else "." if b.max() > 0.3 else " "
                    for b in buckets)
    print("brightness pattern:\n  " + spark)

    mid = np.where((norm > 0.3) & (norm < 0.85))[0]
    if len(mid):
        hs = np.array([colorsys.rgb_to_hsv(*rgb[i]) for i in mid])
        print(f"colour (lit frames): mean sat={hs[:,1].mean():.2f}  "
              f"hue(deg) p20..p80={np.percentile(hs[:,0]*360,20):.0f}..{np.percentile(hs[:,0]*360,80):.0f}")


if __name__ == "__main__":
    main()
