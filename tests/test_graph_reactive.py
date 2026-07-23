"""Extreme, a direct 'the song is a graph' renderer.

Each lamp reflects its own slice of the spectrum: a glow proportional to that
band's loudness and a flash proportional to a fresh attack (peak) in it.
Instruments separate by frequency across the room, peaks flash in proportion to
their height, a sustained tone only glows (never strobes), and the beat grid is
ignored entirely — see EffectEngine._render_extreme.

The enrichment on top of the original v1.40 build (see the tests at the bottom):
  * every hit of a STEADY groove keeps flashing (per-bin flux), not just the
    first, surprising peak the slow-baseline transient would absorb, and
  * the lamp<->spectrum map ROTATES over time, so every lamp takes turns on
    every instrument (the whole room plays the song) while the spectrum stays
    fully covered at every instant.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import MELBANK_BINS, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 6) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _frame(
    melbank: list[float], energy: float = 0.5, ref: list[float] | None = None
) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.4, "bass": 0.4, "low_mid": 0.4, "mid": 0.4, "high": 0.4},
        energy=energy, melbank=list(melbank),
        melbank_ref=list(ref) if ref else [],
    )


def _eng() -> EffectEngine:
    e = EffectEngine(_channels(6))
    e.set_mode(SyncMode.EXTREME)
    return e


def test_instruments_separate_by_frequency_across_the_room():
    # A low (bass) tone lights the LOW lamps (left) brighter than the high lamps;
    # a high (treble) tone does the opposite — instruments separate in space.
    # Measured early (glow settles in ~10 frames): the spectral map rotates over
    # time, so instantaneous frequency→position separation holds near the start
    # before rotation carries the bands around the room.
    low = [0.9] * 4 + [0.0] * (MELBANK_BINS - 4)
    high = [0.0] * (MELBANK_BINS - 4) + [0.9] * 4
    eng = _eng()
    for _ in range(12):
        out = eng.render(_frame(low), _DT)
    assert max(out[0]) > max(out[5]) + 0.15    # bass → low lamp brightest

    eng = _eng()
    for _ in range(12):
        out = eng.render(_frame(high), _DT)
    assert max(out[5]) > max(out[0]) + 0.15    # treble → high lamp brightest


def test_flash_scales_with_peak_height():
    # A bigger transient (peak) flashes brighter than a smaller one — reactions
    # are in direct proportion to how far the graph jumped up.
    def flash_for(jump: float) -> float:
        eng = _eng()
        base = [0.2] * MELBANK_BINS
        for _ in range(40):  # settle the per-bin baseline
            eng.render(_frame(base), _DT)
        peak = 0.0
        peaked = [0.2 + jump] * MELBANK_BINS
        for _ in range(6):  # the transient jump + its brief decay
            out = eng.render(_frame(peaked), _DT)
            peak = max(peak, max(max(c) for c in out.values()))
        return peak

    assert flash_for(0.7) > flash_for(0.2) + 0.15


def test_sustained_tone_glows_without_strobing():
    # A held tone (no fresh transient) gives a steady GLOW, not a strobe.
    eng = _eng()
    rooms = []
    for _ in range(120):
        out = eng.render(_frame([0.5] * MELBANK_BINS), _DT)
        rooms.append(max(max(c) for c in out.values()))
    r = np.array(rooms[40:])
    assert r.mean() > 0.15                 # a real lit glow (tracks loudness)
    assert float(np.std(np.diff(r))) < 0.02  # smooth — no strobing


def test_ignores_the_beat_grid_entirely():
    # A locked grid predicting beats must add NOTHING over a held tone: the graph
    # renderer never reads the grid, so there are no phantom/predicted beats.
    grid = BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5,
        predicted_beat=True, accent=1.0, accent_now=1.0,
    )
    mel = [0.4] * MELBANK_BINS
    eng = _eng()
    with_grid = []
    for i in range(60):
        out = eng.render(_frame(mel), _DT, beatgrid=grid)
        if i >= 40:
            with_grid.append(max(max(c) for c in out.values()))
    eng = _eng()
    no_grid = []
    for i in range(60):
        out = eng.render(_frame(mel), _DT)
        if i >= 40:
            no_grid.append(max(max(c) for c in out.values()))
    assert abs(np.mean(with_grid) - np.mean(no_grid)) < 1e-6


def test_fades_out_in_silence():
    eng = _eng()
    for _ in range(40):
        eng.render(_frame([0.5] * MELBANK_BINS), _DT)
    out = None
    for _ in range(60):  # audio stops
        out = eng.render(_frame([0.0] * MELBANK_BINS, energy=0.0), _DT)
    assert max(max(c) for c in out.values()) < 0.05  # rests dark


def test_steady_groove_keeps_flashing_every_hit():
    # A busy, steady pattern (each hit riding on sustained energy) must keep
    # flashing on EVERY hit — the room follows the whole groove, not just the
    # first, surprising one. The slow-baseline transient climbs into such a
    # pattern and fades; the per-bin flux re-fires each attack, so the later hits
    # stay as bright as the first.
    eng = _eng()
    rest = [0.55] * MELBANK_BINS
    hit = [0.9] * MELBANK_BINS
    for _ in range(20):  # settle the baseline into the sustained bed
        eng.render(_frame(rest), _DT)
    peaks = []
    for _ in range(16):
        out = eng.render(_frame(hit), _DT)  # the attack
        peaks.append(max(max(c) for c in out.values()))
        eng.render(_frame(rest), _DT)  # back to the bed
    assert min(peaks[2:]) > 0.3  # every hit past warm-up still flashes
    assert peaks[-1] > 0.7 * max(peaks)  # the groove does not fade out over time


def test_loud_bands_outshine_quiet_bands_of_equal_activity():
    # Per-band absolute loudness: two bands with EQUAL activity but different
    # absolute loudness (melbank_ref) light their lamps differently — the loud
    # band (a kick) brighter than the quiet one (a faint cymbal). With no ref the
    # per-bin-normalised activity lights them equally; the ref restores hierarchy.
    n = MELBANK_BINS
    activity = [0.8] * n
    ref = [1.0] * (n // 2) + [0.25] * (n - n // 2)  # lows loud, highs quiet

    def low_high(with_ref: bool):
        eng = _eng()
        out = {}
        for _ in range(12):  # settle the glow; rotation is still ~0 here
            out = eng.render(_frame(activity, ref=ref if with_ref else None), _DT)
        low = max(max(out[c]) for c in (0, 1))   # low-frequency (loud) lamps
        high = max(max(out[c]) for c in (3, 4))  # high-frequency (quiet) lamps
        return low, high

    low_w, high_w = low_high(with_ref=True)
    assert low_w > high_w + 0.12          # loud band clearly outshines the quiet one
    low_f, high_f = low_high(with_ref=False)
    assert abs(low_f - high_f) < 0.08     # without the ref, equal activity == equal


def test_spectrum_rotates_across_lamps_over_time():
    # A FIXED low tone must not stay pinned to one lamp forever: the spectral map
    # rotates, so over time different lamps respond to the same low band — every
    # lamp takes turns — while the low band is always represented somewhere
    # (full coverage) and the rotation is driven by time, never a beat grid.
    low = [0.9] * 3 + [0.0] * (MELBANK_BINS - 3)
    eng = _eng()
    seen: set[int] = set()
    lit_everywhere = True
    for i in range(1400):  # ~28 s at 20 ms — several lamp-steps of rotation
        out = eng.render(_frame(low), _DT)
        vals = {cid: max(c) for cid, c in out.items()}
        if i >= 40:  # after warm-up
            seen.add(max(vals, key=vals.get))  # which lamp owns the low band now
            if max(vals.values()) < 0.15:
                lit_everywhere = False
    assert len(seen) >= 3  # the low band visits multiple lamps around the room
    assert lit_everywhere  # and is always lit somewhere (coverage preserved)
