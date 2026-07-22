"""Extreme flashes only on REAL onsets, not phantom scheduled beats.

An offline track map force-fits a tempo grid across the WHOLE song, so it keeps
scheduling beats through a tail / breakdown / vocal passage after the drums have
stopped — a metronome that never quits. Extreme's onset-flux gate mutes any beat
whose frame carries no real onset flux (a held tone / vocal has ~none, a real
drum spikes), so those "predictive" beats don't flash or jump colour — only
genuine transient peaks do. The gate is Extreme-only (flux_gate 0 elsewhere).
"""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _locked_grid(predicted: bool) -> BeatGrid:
    return BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5, phase=0.0,
        time_to_next_beat=0.5, next_beat_t=0.0, bar_phase=0.0,
        predicted_beat=predicted, accent=1.0, accent_now=1.0, beat_in_bar=0,
    )


def _frame(bass_flux: float) -> AnalysisFrame:
    # A loud, sustained sound; the "beat" comes only from the scheduled grid.
    return AnalysisFrame(
        bands={"sub_bass": 0.6, "bass": 0.6, "low_mid": 0.4, "mid": 0.4, "high": 0.3},
        energy=0.7, salience=1.0, onset_width=1.0, bass_flux=bass_flux,
    )


def _peak_over_run(mode: SyncMode, bass_flux: float) -> float:
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    peak = 0.0
    for i in range(80):
        out = eng.render(_frame(bass_flux), _DT, beatgrid=_locked_grid(i % 25 == 0))
        peak = max(peak, max(max(c) for c in out.values()))
    return peak


def test_extreme_mutes_scheduled_beats_without_onset_flux():
    # Phantom scheduled beats over a held tone (no onset flux) vs the same beats
    # with a real flux spike: only the real onsets light the room.
    phantom = _peak_over_run(SyncMode.EXTREME, bass_flux=0.0)
    real = _peak_over_run(SyncMode.EXTREME, bass_flux=1.0)
    assert real > 0.6                # a real onset flashes hard
    assert phantom < real - 0.4      # a phantom beat barely lifts the room


def test_flux_gate_is_extreme_only():
    # Intense keeps flux_gate 0: its scheduled beats fire with or without flux
    # (the gate is a provable no-op there), so Intense is unchanged.
    assert MODE_PARAMS[SyncMode.INTENSE].flux_gate == 0.0
    assert _peak_over_run(SyncMode.INTENSE, bass_flux=0.0) > 0.5
