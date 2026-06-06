#!/usr/bin/env python3
"""Milestone 2 spike: validate the audio tap + analysis pipeline.

Decodes an audio URL (a Music Assistant stream URL, or any local/HTTP audio
file) with ffmpeg to mono PCM and runs the integration's real ``Analyzer`` over
it, printing beat counts, tempo and per-band levels. This confirms ffmpeg + the
DSP work on the target host before wiring it into Home Assistant.

Finding a Music Assistant stream URL to test with: start playback in MA, then in
the MA web UI look at the player, or check the MA server log for the
``/single/{token}/{player}/{item}`` URL it hands to the player, and pass it here.

Usage:
    python spike_ma.py --url http://ma-host:8097/single/<token>/<player>/<item>
    python spike_ma.py --url song.flac --seconds 20
"""

from __future__ import annotations

import argparse
import enum
import os
import subprocess
import sys
import time

import numpy as np

# StrEnum shim for running under Python < 3.11 (HA itself is 3.13).
if not hasattr(enum, "StrEnum"):
    class _StrEnum(str, enum.Enum):
        def __str__(self) -> str:  # pragma: no cover
            return self.value
    enum.StrEnum = _StrEnum  # type: ignore[attr-defined]


def _load_analyzer(components_path: str):
    sys.path.insert(0, components_path)
    from hue_music_sync.audio.analyzer import Analyzer  # noqa: E402
    from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE  # noqa: E402

    return Analyzer, ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    default_cc = os.path.join(os.path.dirname(here), "custom_components")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="audio file or HTTP(S) stream URL")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--components-path", default=default_cc)
    p.add_argument("--ffmpeg", default="ffmpeg")
    a = p.parse_args()

    Analyzer, HOP, SR = _load_analyzer(a.components_path)
    analyzer = Analyzer()

    proc = subprocess.Popen(
        [
            a.ffmpeg, "-nostdin", "-loglevel", "error", "-i", a.url,
            "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1",
        ],
        stdout=subprocess.PIPE,
    )

    hop_bytes = HOP * 2
    frames = 0
    beats = 0
    band_acc = None
    last_print = time.monotonic()
    start = time.monotonic()
    last_tempo = None
    try:
        while time.monotonic() - start < a.seconds:
            raw = proc.stdout.read(hop_bytes)
            if len(raw) < hop_bytes:
                break
            hop = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            f = analyzer.push(hop)
            frames += 1
            beats += int(f.beat)
            last_tempo = f.tempo_bpm
            vals = np.array([f.bands[k] for k in sorted(f.bands)])
            band_acc = vals if band_acc is None else band_acc + vals
            now = time.monotonic()
            if now - last_print >= 1.0:
                bar = " ".join(f"{k[:4]}={f.bands[k]:.2f}" for k in sorted(f.bands))
                print(f"t={frames*HOP/SR:5.1f}s  beats={beats:3d}  "
                      f"bpm={f.tempo_bpm or 0:5.1f}  {bar}  E={f.energy:.2f}")
                last_print = now
    finally:
        proc.terminate()

    if frames:
        avg = band_acc / frames
        secs = frames * HOP / SR
        print("\n--- summary ---")
        print(f"analyzed {secs:.1f}s, {frames} frames")
        print(f"beats detected: {beats}  (~{beats/secs*60:.0f}/min)  tempo est: {last_tempo}")
        for k, v in zip(sorted(analyzer._band_bins), avg):
            print(f"  mean {k:8s} = {v:.3f}")
    else:
        print("No audio decoded — check the URL/ffmpeg.")


if __name__ == "__main__":
    main()
