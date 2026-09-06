"""Sustain bloom + loudness-aware melbank (P3, ported from CAMusic).

The bloom is the tonal mirror of the event gates: a held, pitched, mid-heavy
sound (a vocal, a pad) lifts the whole room slowly instead of reading dark,
while percussion — broadband, transient-rich — does not bloom. The melbank
shape knobs (mean+peak blend, mean-normalised absolute-loudness weights) make
a lamp's slice of the spectrum read at its real height and loudness.
"""

from __future__ import annotations

import pytest

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 1.0 / 50.0


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _vocal_frame(t: int) -> AnalysisFrame:
    """A held vocal: narrowband onset, mid-heavy, steady pitch, no transient."""
    chroma = [0.0] * 12
    chroma[t % 1] = 1.0  # constant pitch class (C) every frame
    return AnalysisFrame(
        bands={"sub_bass": 0.1, "bass": 0.1, "low_mid": 0.5, "mid": 0.6, "high": 0.2},
        energy=0.5,
        melbank=[0.15] * 16,
        salience=1.0,
        onset_width=0.10,
        chroma=chroma,
    )


def _kick_frame(t: int) -> AnalysisFrame:
    """Percussion: broadband onsets and a transient that keeps re-firing."""
    mel = [0.1] * 16
    if t % 4 == 0:
        mel[0:4] = [1.0] * 4  # a kick slams the low bins
    return AnalysisFrame(
        bands={"sub_bass": 0.8, "bass": 0.8, "low_mid": 0.2, "mid": 0.2, "high": 0.1},
        energy=0.7,
        melbank=mel,
        salience=1.0,
        onset_width=0.90,
        chroma=[0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )


def _run(mode: SyncMode, frames, n: int) -> tuple[EffectEngine, float]:
    """Render ``n`` frames; return (engine, final field level).

    The bloom is asserted via ``engine.tonal_env()`` — the layer under
    test, isolated from the music's own continuous energy (a kick frame
    is *louder* than a vocal frame in every other layer by design).
    """
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    field = 0.0
    for i in range(n):
        out = eng.render(frames(i), _DT)
        field = max(max(c) for c in out.values())
    return eng, field


def test_held_vocal_blooms_the_room():
    # The whole point of the layer: a sustained vocal/pad lifts the room well
    # beyond its starting glow, on the tonal time constants (bloom, not pulse).
    _, early = _run(SyncMode.MEDIUM, _vocal_frame, 15)  # 0.3 s in
    eng, late = _run(SyncMode.MEDIUM, _vocal_frame, 150)  # 3 s in
    assert eng.tonal_env() > 0.4, "the bloom envelope never committed"
    assert late > early + 0.05, (
        f"a held vocal did not bloom: {early} -> {late}"
    )


def test_percussion_does_not_bloom():
    # The mirror-image guarantee: broadband, transient-rich material is the
    # event gates' territory — the bloom must stay near zero there.
    vocal_eng, _ = _run(SyncMode.MEDIUM, _vocal_frame, 150)
    kick_eng, _ = _run(SyncMode.MEDIUM, _kick_frame, 150)
    assert vocal_eng.tonal_env() > 0.4
    assert kick_eng.tonal_env() < 0.02, (
        f"percussion bloomed to {kick_eng.tonal_env()} "
        f"vs the vocal's {vocal_eng.tonal_env()}"
    )


def test_chroma_churn_gates_the_bloom():
    # A held PITCH blooms; a narrowband wash with constantly moving pitch
    # content (a busy melodic line) must not — chroma stability is what
    # separates a vocal from room tone.
    churn = [0.0] * 12

    def _churning(t: int) -> AnalysisFrame:
        f = _vocal_frame(t)
        # rotate the pitch class every frame: maximal harmonic movement
        churn[t % 12] = 1.0
        f.chroma[:] = [v for v in churn]
        f.chroma[(t + 6) % 12] = 1.0
        f.chroma[t % 12] = 0.0
        return f

    steady_eng, _ = _run(SyncMode.MEDIUM, _vocal_frame, 150)
    busy_eng, _ = _run(SyncMode.MEDIUM, _churning, 150)
    steady = steady_eng.tonal_env()
    busy = busy_eng.tonal_env()
    assert busy < steady - 0.05, (
        f"chroma churn did not gate the bloom: {steady} -> {busy}"
    )


def test_bloom_is_silence_gated():
    # The bloom rides the same silence gate as everything else: a paused or
    # silent track must never drift bright on its own.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.MEDIUM)
    silent = AnalysisFrame(bands={}, energy=0.0, melbank=[], salience=1.0)
    for i in range(120):
        eng.render(silent, _DT)
    assert eng.tonal_env() == pytest.approx(0.0, abs=1e-6)


def test_loudness_weights_redistribute_not_attenuate():
    # Mean-normalisation is the shape guarantee: the weights redistribute
    # brightness between bands (mean ~1) instead of dimming the whole melbank
    # (the raw form is a pure attenuator, kept for Extreme's tuned gains).
    from hue_music_sync.effects.engine import _LOUD_WEIGHT_MAX, _LOUD_WEIGHT_MIN

    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.MEDIUM)  # band_loud_strength = 0.35
    mel = [0.5] * 16
    ref = [1.0] * 4 + [0.25] * 12  # bass loud, highs quiet
    w = eng.melbank_loud_weights(
        AnalysisFrame(melbank=mel, melbank_ref=ref), eng.params
    )
    assert w is not None
    assert sum(w) / len(w) == pytest.approx(1.0, rel=0.02)
    assert min(w) >= _LOUD_WEIGHT_MIN and max(w) <= _LOUD_WEIGHT_MAX
    # The ordering survives: the loud band is still the heavier weight.
    assert w[0] > w[8]


def test_loudness_weights_absent_without_a_reference():
    # Empty / mismatched melbank_ref (live taps, metadata frames, pre-v5 maps)
    # means "not computed": uniform, exactly as before the layer existed.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.MEDIUM)
    assert eng.melbank_loud_weights(AnalysisFrame(melbank=[0.5] * 16), eng.params) is None
    assert (
        eng.melbank_loud_weights(
            AnalysisFrame(melbank=[0.5] * 16, melbank_ref=[1.0] * 8), eng.params
        )
        is None
    )


def test_mel_peakiness_blends_mean_toward_the_hottest_bin():
    # A held vocal occupies 2-3 of a lamp's ~7 bins, so a pure mean delivered
    # it at a third of its real height. Peakiness restores the slice's peak.
    from hue_music_sync.effects.modes import _melbank_drive

    info = {"mel_lo": 0, "mel_hi": 8, "nx": 0.5, "band": "mid"}
    env = {}
    mel = [0.1] * 7 + [1.0]
    plain = _melbank_drive(AnalysisFrame(melbank=mel), env, info, 0.0)
    peaky = _melbank_drive(
        AnalysisFrame(melbank=mel), env, info, 0.0, 0.40, None
    )
    assert plain == pytest.approx(sum(mel) / 8)  # default is the old plain mean
    assert peaky == pytest.approx(0.6 * plain + 0.4 * 1.0)
    assert peaky > plain


def test_contrast_tunable_reaches_mel_peakiness():
    # The ``contrast`` tunable shapes the mean+peak blend too (from CAMusic):
    # higher contrast favours the hottest bin in a lamp's slice.
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.MEDIUM)
    base = eng.params.mel_peakiness
    eng.set_tunables({"contrast": 1.5})
    assert eng.params.mel_peakiness == pytest.approx(min(1.0, base * 1.5))
    eng.set_tunables(None)
    assert eng.params.mel_peakiness == pytest.approx(base)
