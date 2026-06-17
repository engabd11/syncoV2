#!/usr/bin/env python3
"""Offline liveliness harness for the LedFx-style continuous engine.

Runs the *full live pipeline* — Analyzer -> TempoTracker -> StructureTracker ->
EffectEngine.render — over a real recording (or a synthetic click+bed when no
file is given) and reports, per intensity mode, whether the room stays alive:

    python sim_show.py "<audio file or URL>"
    python sim_show.py                # synthetic test signal

For each mode it prints, over the frames where music is actually playing
(energy above the analyzer's noise gate):

  dark%   fraction of frames the whole room is below ~darkness (0.12)
  bri     mean / 10th-percentile room brightness (the continuous floor)
  move    mean frame-to-frame brightness change (reactivity; flat == dead)
  colour  palette phase advanced per second (colour motion)

and crucially repeats the run with the beat grid FORCED UNLOCKED (beatgrid=None
every frame) to prove the core invariant: with no beats at all the show must
still be lively and no darker than the locked run's continuous floor. A missed
or mistimed beat must only remove *punch*, never the light.
"""

from __future__ import annotations

import enum
import math
import os
import subprocess
import sys
import types

import numpy as np

if not hasattr(enum, "StrEnum"):
    class _S(str, enum.Enum):
        def __str__(self):  # noqa: D401
            return self.value
    enum.StrEnum = _S  # type: ignore[attr-defined]
_CC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components"
)
if "hue_music_sync" not in sys.modules:
    _pkg = types.ModuleType("hue_music_sync")
    _pkg.__path__ = [os.path.join(_CC, "hue_music_sync")]
    sys.modules["hue_music_sync"] = _pkg

from hue_music_sync.audio.analyzer import Analyzer  # noqa: E402
from hue_music_sync.audio.structure import StructureTracker  # noqa: E402
from hue_music_sync.audio.tempo import TempoTracker  # noqa: E402
from hue_music_sync.const import (  # noqa: E402
    ANALYSIS_HOP,
    ANALYSIS_NOISE_FLOOR,
    ANALYSIS_SAMPLE_RATE,
    SyncMode,
)
from hue_music_sync.audio.trackmap import analyze_pcm  # noqa: E402
from hue_music_sync.effects.engine import EffectEngine  # noqa: E402


class Ch:
    """Minimal entertainment-channel stand-in (avoids the aiohttp import)."""

    __slots__ = ("channel_id", "x", "y", "z")

    def __init__(self, channel_id: int, x: float, y: float, z: float) -> None:
        self.channel_id = channel_id
        self.x, self.y, self.z = x, y, z


def _channels(n: int = 6) -> list[Ch]:
    # Spread left-right (drives the melbank "wavelength" mapping) and in height.
    return [
        Ch(i, -1.0 + 2.0 * i / (n - 1), 0.0, (i % 3) / 2.0)
        for i in range(n)
    ]


def _decode(url: str) -> np.ndarray | None:
    args = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", url,
        "-t", "120", "-vn", "-ac", "1", "-ar", str(ANALYSIS_SAMPLE_RATE),
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    try:
        out = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
    except OSError as err:
        print(f"ffmpeg failed: {err}")
        return None
    return np.frombuffer(out, dtype="<f4").copy() if out else None


def _synthetic(seconds: float = 30.0, bpm: float = 124.0) -> np.ndarray:
    """A kick four-to-the-floor + a fluctuating mid/high bed + a quiet break."""
    sr = ANALYSIS_SAMPLE_RATE
    n = int(sr * seconds)
    t = np.arange(n) / sr
    rng = np.random.default_rng(7)
    bed = rng.standard_normal(n).astype(np.float32) * 0.15
    bed *= (0.6 + 0.4 * np.sin(2 * np.pi * 0.3 * t)).astype(np.float32)
    sig = bed
    period = 60.0 / bpm
    env = np.exp(-np.arange(int(0.12 * sr)) / (0.035 * sr))
    kick = (np.sin(2 * np.pi * 55 * np.arange(len(env)) / sr) * env).astype(np.float32)
    for bt in np.arange(0.3, seconds, period):
        if 12.0 < bt < 18.0:  # a breakdown: kicks drop out
            continue
        i = int(bt * sr)
        seg = kick[: n - i]
        sig[i : i + len(seg)] += seg
    sig[int(13 * sr):int(17 * sr)] *= 0.12  # near-silent break
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak * 0.9).astype(np.float32)


def _run(pcm: np.ndarray, mode: SyncMode, force_unlocked: bool):
    a = Analyzer()
    tt = TempoTracker(a.frame_period)
    st = StructureTracker(a.frame_period)
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    period = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE

    room = []           # room brightness (max lamp) per music frame
    roommean = []       # average lamp brightness per music frame
    colour0 = eng.colour_phase
    music_frames = 0
    nbeats = 0
    for k in range(len(pcm) // ANALYSIS_HOP):
        f = a.push(pcm[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        grid = tt.update(
            f.t_audio, f.flux, f.beat, f.beat_strength,
            bass=max(f.bands.get("sub_bass", 0.0), f.bands.get("bass", 0.0)),
        )
        structure = st.update(f)
        if grid is not None and grid.locked and grid.predicted_beat:
            nbeats += 1
        out = eng.render(f, period, None if force_unlocked else grid, structure)
        rms = float(np.sqrt(np.mean(pcm[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP] ** 2)))
        if rms < ANALYSIS_NOISE_FLOOR:
            continue  # genuine silence: not counted (room is allowed to be dark)
        music_frames += 1
        vals = [max(c) for c in out.values()]
        room.append(max(vals))
        roommean.append(sum(vals) / len(vals))
    if not room:
        return None
    room = np.array(room)
    rmean = np.array(roommean)
    move = float(np.mean(np.abs(np.diff(room)))) if room.size > 1 else 0.0
    # Whole-room pulse: how much the AVERAGE lamp swings (so a single flashing
    # lamp can't fake it - this is the "does the room pulse together" measure).
    mmove = float(np.mean(np.abs(np.diff(rmean)))) if rmean.size > 1 else 0.0
    colour_rate = (eng.colour_phase - colour0) / (music_frames * period)
    return {
        "dark": float(np.mean(room < 0.12)),
        "mean": float(room.mean()),
        "rmean": float(rmean.mean()),
        "move": move,
        "mmove": mmove,
        "colour": colour_rate,
        "beats": nbeats,
        "frames": music_frames,
    }


def main() -> int:
    if len(sys.argv) >= 2:
        pcm = _decode(sys.argv[1])
        label = os.path.basename(sys.argv[1])
        if pcm is None or pcm.size < ANALYSIS_SAMPLE_RATE:
            print("decode failed or too short; using synthetic signal")
            pcm, label = _synthetic(), "synthetic"
    else:
        pcm, label = _synthetic(), "synthetic"
    print(f"signal: {label}  ({pcm.size / ANALYSIS_SAMPLE_RATE:.1f}s)\n")

    hdr = (f"{'mode':8} {'grid':9} {'dark%':>6} {'maxbri':>6} {'roombr':>6} "
           f"{'move':>6} {'roommv':>6} {'col/s':>6} {'beats':>6}")
    print(hdr)
    print("-" * len(hdr))
    ok = True
    for mode in SyncMode:
        locked = _run(pcm, mode, force_unlocked=False)
        unlocked = _run(pcm, mode, force_unlocked=True)
        if locked is None:
            continue
        for tag, m in (("locked", locked), ("unlocked", unlocked)):
            print(
                f"{mode.value:8} {tag:9} {m['dark']*100:6.1f} {m['mean']:6.2f} "
                f"{m['rmean']:6.2f} {m['move']:6.3f} {m['mmove']:6.3f} "
                f"{m['colour']:6.2f} {m['beats']:6d}"
            )
        # Core invariant: with the grid forced unlocked the room must stay alive
        # — reactive (brightness keeps moving) and no darker overall than the
        # locked run (beats only ADD punch, they never gate the light). Subtle
        # is steady by design (no dimming), so it is reported, not failed.
        if mode is SyncMode.SUBTLE:
            verdict = "STEADY"
        else:
            reactive = unlocked["move"] > 0.004
            not_darker = (
                unlocked["mean"] >= locked["mean"] - 0.06
                and unlocked["dark"] <= locked["dark"] + 0.08
            )
            alive = reactive and not_darker
            verdict = "ALIVE" if alive else "DEAD?"
            if not alive:
                ok = False
        print(f"{'':8} {'-> ' + verdict}")
    print("\nINVARIANT:", "PASS" if ok else "FAIL (a mode goes dead when unlocked)")

    # --- scheduled (track-map) path: the "metadata from music" show -----------
    tm = analyze_pcm(pcm)
    if tm is not None and tm.features is not None:
        mel = tm.features.melbank
        print(f"\ntrack map: bpm {tm.bpm:.1f}  conf {tm.confidence:.2f}  "
              f"{'USABLE' if tm.usable else 'live-fallback'}  "
              f"melbank {('%dx%d' % mel.shape) if mel.size else 'MISSING'}")
        for s in tm.sections:
            swatch = " ".join(
                "#%02x%02x%02x" % tuple(int(max(0.0, min(1.0, v)) * 255) for v in c)
                for c in s.palette
            ) or "(none)"
            print(f"  section {s.start:6.1f}-{s.end:6.1f}s  e{s.energy:.2f}  song colours: {swatch}")
        # Scheduled playback should be as alive as the live path.
        ok_sched = _run_scheduled(tm, SyncMode.INTENSE)
        print("SCHEDULED-PLAYBACK LIVELINESS:", "PASS" if ok_sched else "FAIL")
        ok = ok and ok_sched
    return 0 if ok else 1


def _run_scheduled(tm, mode: SyncMode) -> bool:
    """Render the track map's own frames through the engine (no live audio)."""
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    period = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE
    room = []
    pos, prev = float(tm.beats[0]) if tm.beats.size else 1.0, None
    for _ in range(int(min(60.0, tm.duration) / period)):
        frame = tm.frame_at(pos, prev)
        if frame is None:
            break
        out = eng.render(frame, period, beatgrid=None)  # worst case: no grid
        room.append(max(max(c) for c in out.values()))
        prev = pos
        pos += period
    if not room:
        return False
    room = np.array(room)
    move = float(np.mean(np.abs(np.diff(room)))) if room.size > 1 else 0.0
    print(f"  scheduled INTENSE: dark {float((room < 0.12).mean())*100:.1f}%  "
          f"bri {room.mean():.2f}  move {move:.3f}")
    return room.mean() > 0.30 and move > 0.004


if __name__ == "__main__":
    sys.exit(main())
