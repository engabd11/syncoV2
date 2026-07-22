"""The onset detector must keep up with fast double-bass / blast-beat metal.

The bass-onset refractory used to be 6 frames (~120 ms), capping detection at
~8.3 onsets/s — so a 12/s double-bass run was detected at only ~5.7/s and half
the kicks never flashed (they showed as a dim continuous wiggle, the "fast beats
almost not showing" complaint). ONSET_REFRACTORY_FRAMES is now 3 (~60 ms), and
the rising-edge guard in the detector still stops a single hit's decay tail from
re-triggering, so real fast runs register without over-detecting slower content.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer
from hue_music_sync.const import ANALYSIS_HOP as HOP, ANALYSIS_SAMPLE_RATE as SR

_DUR = 6.0


def _kick_burst(kps: float, seconds: float = _DUR) -> tuple[np.ndarray, int]:
    """A 55 Hz kick burst repeated `kps` times/second over a light noise bed."""
    n = int(SR * seconds)
    rng = np.random.default_rng(1)
    sig = rng.standard_normal(n).astype(np.float32) * 0.05
    env = np.exp(-np.arange(int(0.05 * SR)) / (0.012 * SR)).astype(np.float32)
    kick = (np.sin(2 * np.pi * 55 * np.arange(len(env)) / SR) * env).astype(np.float32)
    kicks = 0
    for bt in np.arange(0.2, seconds, 1.0 / kps):
        i = int(bt * SR)
        seg = kick[: n - i]
        sig[i : i + len(seg)] += seg * 1.2
        kicks += 1
    sig /= max(1e-6, float(np.max(np.abs(sig))))
    return (sig * 0.9).astype(np.float32), kicks


def _detected(sig: np.ndarray) -> int:
    a = Analyzer()
    return sum(1 for k in range(len(sig) // HOP)
               if a.push(sig[k * HOP : (k + 1) * HOP]).bass_beat)


def test_fast_double_bass_is_detected():
    # 12 kicks/s: the analyzer must now catch nearly all of them (was ~5.7/s
    # under the old 120 ms refractory).
    sig, kicks = _kick_burst(12.0)
    det = _detected(sig)
    assert det / _DUR > 10.0, f"only {det/_DUR:.1f}/s of ~12/s detected"
    assert det >= 0.8 * kicks  # most of the real kicks land


def test_slow_kick_is_not_over_detected():
    # A slow four-to-floor (2/s) must detect ~one onset per kick — the shorter
    # refractory doesn't invent extra onsets on well-spaced hits.
    sig, kicks = _kick_burst(2.0)
    det = _detected(sig)
    assert kicks - 2 <= det <= kicks + 2
