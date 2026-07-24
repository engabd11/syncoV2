"""Advanced live tunables: percentage multipliers on the active mode's params.

Each knob scales whatever the current mode actually uses (and no-ops on a param
the mode leaves at 0), so one set of knobs works across every intensity. 1.0 (or
no tunables at all) reproduces the mode's coded values exactly.
"""

from __future__ import annotations

from hue_music_sync.const import SyncMode, sanitize_tunables
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS
from hue_music_sync.hue.bridge import EntertainmentChannel


def _eng(mode: SyncMode = SyncMode.EXTREME) -> EffectEngine:
    e = EffectEngine([EntertainmentChannel(channel_id=i, x=-1.0 + i, y=0.0, z=0.0)
                      for i in range(3)])
    e.set_mode(mode)
    return e


def test_no_tunables_is_the_coded_preset():
    e = _eng()
    assert e.params is MODE_PARAMS[SyncMode.EXTREME]
    e.set_tunables({})  # empty stays the exact preset
    assert e.params is MODE_PARAMS[SyncMode.EXTREME]
    e.set_tunables({"reactivity": 1.0})  # all-1.0 changes nothing
    assert e.params.spectral_pop == MODE_PARAMS[SyncMode.EXTREME].spectral_pop


def test_reactivity_scales_flash_gains():
    base = MODE_PARAMS[SyncMode.EXTREME]
    e = _eng()
    e.set_tunables({"reactivity": 2.0})
    assert e.params.spectral_pop == base.spectral_pop * 2.0
    assert e.params.mel_flux_gain == base.mel_flux_gain * 2.0


def test_movement_and_glow_and_loudness_scale():
    base = MODE_PARAMS[SyncMode.EXTREME]
    e = _eng()
    e.set_tunables({"movement": 0.5, "glow": 1.5, "loudness": 2.0})
    assert e.params.rotate_rate == base.rotate_rate * 0.5
    assert e.params.rotate_swing == base.rotate_swing * 0.5
    assert e.params.melbank_gain == base.melbank_gain * 1.5
    # band_loud_strength is a 0..1 quantity, so it clamps rather than exceed 1.
    assert e.params.band_loud_strength == min(1.0, base.band_loud_strength * 2.0)


def test_tunables_survive_a_mode_change():
    # The auto picker changes mode via set_mode mid-song; the live knobs must
    # carry over rather than snap back to the new mode's coded values.
    e = _eng(SyncMode.INTENSE)
    e.set_tunables({"reactivity": 1.5})
    e.set_mode(SyncMode.EXTREME)
    assert e.params.spectral_pop == MODE_PARAMS[SyncMode.EXTREME].spectral_pop * 1.5


def test_knob_noops_on_a_param_the_mode_does_not_use():
    # Extreme leaves beat_gain at 0; scaling reactivity can't resurrect it.
    e = _eng(SyncMode.EXTREME)
    e.set_tunables({"reactivity": 2.0})
    assert e.params.beat_gain == 0.0


def test_sanitize_tunables_clamps_and_filters():
    out = sanitize_tunables(
        {"reactivity": 3.0, "glow": -1.0, "bogus": 5.0, "movement": "0.7"}
    )
    assert out == {"reactivity": 2.0, "glow": 0.0, "movement": 0.7}
    assert sanitize_tunables("not a dict") == {}
