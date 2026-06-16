#!/usr/bin/env python3
"""Dump the offline track map (tempo, beats, sections) for a local audio file.

Sanity-check the Hue×Spotify-style analysis against a real song before testing
in Home Assistant:

    python analyze_track.py "<audio file or URL>"
"""

from __future__ import annotations

import enum
import os
import sys
import types

if not hasattr(enum, "StrEnum"):
    class _S(str, enum.Enum):
        def __str__(self): return self.value
    enum.StrEnum = _S  # type: ignore[attr-defined]
_CC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components")
if "hue_music_sync" not in sys.modules:
    _pkg = types.ModuleType("hue_music_sync"); _pkg.__path__ = [os.path.join(_CC, "hue_music_sync")]
    sys.modules["hue_music_sync"] = _pkg

from hue_music_sync.audio.trackmap import _decode_and_analyze  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    tm = _decode_and_analyze("ffmpeg", url, max_seconds=720)
    if tm is None:
        print("analysis failed (couldn't decode, or track too short/quiet)")
        return 1
    print(f"duration   : {tm.duration:8.1f} s")
    print(f"tempo      : {tm.bpm:8.1f} BPM  (confidence {tm.confidence:.2f}, "
          f"{'USABLE' if tm.usable else 'not usable -> live fallback'})")
    print(f"beats      : {tm.beats.size}  (downbeat offset {tm.downbeat})")
    print(f"mid onsets : {tm.mid_beats.size}  (guitar/snare stream)")
    if tm.beats.size > 8:
        ivals = tm.beats[1:] - tm.beats[:-1]
        print(f"  interval : median {ivals.mean():.3f} s, spread "
              f"{ivals.std() * 1000:.0f} ms")
        print(f"  first 8  : {[round(float(b), 2) for b in tm.beats[:8]]}")
    feats = tm.features
    if feats is not None and feats.melbank.size:
        print(f"melbank    : {feats.melbank.shape[1]} bins x {feats.melbank.shape[0]} frames "
              f"(max {float(feats.melbank.max()):.2f}) -> scheduled players get the LedFx layer")
    print(f"sections   : {len(tm.sections)}")
    for s in tm.sections:
        bar = "#" * int(round(s.energy * 30))
        swatch = " ".join(
            "#%02x%02x%02x" % tuple(int(max(0.0, min(1.0, v)) * 255) for v in c)
            for c in s.palette
        ) or "(no harmonic colour)"
        print(f"  {s.start:7.1f} - {s.end:7.1f} s  energy {s.energy:.2f} {bar}")
        print(f"            song colours: {swatch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
