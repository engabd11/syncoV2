"""The ambient idle "show": a slow wandering glow for a paused/empty room.

Pure-logic guard for the renderer itself (the grace-gating that keeps it off
during between-song gaps lives in the coordinator run loop). Verifies the output
is valid and calm, that its movement fades in with ``intensity``, and that it
respects master brightness.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel


def _channels(n: int = 6) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=(i % 3) / 2.0)
        for i in range(n)
    ]


def _engine() -> EffectEngine:
    eng = EffectEngine(_channels())
    eng.set_brightness(1.0)
    return eng


def _chan_brightness_over_time(eng, cid, intensity, ts):
    return [max(eng.render_idle_show(t, intensity)[cid]) for t in ts]


def test_idle_show_outputs_are_valid_and_calm():
    eng = _engine()
    ts = np.arange(0.0, 20.0, 0.25)
    peak = 0.0
    for t in ts:
        out = eng.render_idle_show(float(t), intensity=1.0)
        assert set(out) == {c.channel_id for c in eng.channels}
        for rgb in out.values():
            assert len(rgb) == 3
            for v in rgb:
                assert 0.0 <= v <= 1.0
            peak = max(peak, max(rgb))
    # It's a dim, non-strobing glow — never slams the room to full white.
    assert peak < 0.6


def test_movement_fades_in_with_intensity():
    # At intensity 0 the room only breathes; the spatial waves (the wandering) add
    # real per-frame motion, so a lamp's brightness varies MORE over time at 1.
    eng = _engine()
    ts = np.arange(0.0, 40.0, 0.5)
    cid = eng.channels[len(eng.channels) // 2].channel_id
    var_calm = float(np.var(_chan_brightness_over_time(eng, cid, 0.0, ts)))
    var_full = float(np.var(_chan_brightness_over_time(eng, cid, 1.0, ts)))
    assert var_full > var_calm
    assert var_full > 1e-4  # the show genuinely moves


def test_idle_show_respects_master_brightness():
    eng = _engine()
    eng.set_brightness(0.0)
    for t in (0.0, 5.0, 13.0):
        out = eng.render_idle_show(t, intensity=1.0)
        assert max(max(rgb) for rgb in out.values()) == 0.0
