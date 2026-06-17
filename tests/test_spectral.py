"""Full-spectrum per-lamp reactivity: every instrument drives the lights.

Each lamp pops on a fresh attack in its own slice of the melbank spectrum, so a
kick lights the low lamps, a snare the low-mids, a guitar/lead the mids and a
cymbal the highs - not just the three named roles.
"""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.const import MELBANK_BINS, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02
_N = MELBANK_BINS


def _channels(n: int = 8) -> list[EntertainmentChannel]:
    # Left-to-right lamps map to low-to-high spectrum (the melbank wavelength).
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _frame(mel: list[float]) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.3, "bass": 0.3, "low_mid": 0.3, "mid": 0.3, "high": 0.3},
        energy=0.6,
        melbank=list(mel),
    )


def _warm(eng: EffectEngine, base: list[float], frames: int = 8) -> None:
    for _ in range(frames):
        eng.render(_frame(base), _DT, beatgrid=None)


def _halves(out, n: int = 8) -> tuple[float, float]:
    low = sum(max(out[c]) for c in range(0, n // 2))
    high = sum(max(out[c]) for c in range(n // 2, n))
    return low, high


def test_high_frequency_attack_favours_the_high_lamps():
    base = [0.1] * _N
    eng = EffectEngine(_channels(8))
    eng.set_mode(SyncMode.HIGH)
    _warm(eng, base)
    spike = list(base)
    for i in range(3 * _N // 4, _N):  # top quarter of the spectrum attacks
        spike[i] = 0.95
    low, high = _halves(eng.render(_frame(spike), _DT, beatgrid=None))
    assert high > low + 0.1  # cymbal/air lit the high lamps, not the low ones


def test_low_frequency_attack_favours_the_low_lamps():
    base = [0.1] * _N
    eng = EffectEngine(_channels(8))
    eng.set_mode(SyncMode.HIGH)
    _warm(eng, base)
    spike = list(base)
    for i in range(0, _N // 4):  # bottom quarter of the spectrum attacks
        spike[i] = 0.95
    low, high = _halves(eng.render(_frame(spike), _DT, beatgrid=None))
    assert low > high + 0.1  # a kick lit the low lamps, not the highs


def test_no_spectral_pop_in_subtle():
    # Subtle is seamless (no dimming/popping); spectral_pop must be off there so
    # a spectral attack does not punch the steady level.
    base = [0.1] * _N
    eng = EffectEngine(_channels(8))
    eng.set_mode(SyncMode.SUBTLE)
    _warm(eng, base)
    before = eng.render(_frame(base), _DT, beatgrid=None)
    spike = list(base)
    for i in range(_N // 2, _N):
        spike[i] = 0.95
    after = eng.render(_frame(spike), _DT, beatgrid=None)
    # Steady: the spike must not meaningfully change Subtle's brightness.
    assert abs(max(after[7]) - max(before[7])) < 0.05
