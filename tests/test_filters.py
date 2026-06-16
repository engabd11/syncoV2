"""The shared LedFx-style ExpFilter primitive."""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.filters import ExpFilter


def test_rise_is_faster_than_decay():
    f = ExpFilter(0.0, alpha_rise=0.9, alpha_decay=0.1)
    # One rising step jumps most of the way; one falling step barely moves.
    up = f.update(1.0)
    assert abs(up - 0.9) < 1e-9
    down = f.update(0.0)
    assert abs(down - 0.81) < 1e-9  # 0.9 -> 0.9*0.9, gentle release


def test_converges_to_steady_input():
    f = ExpFilter(0.0, alpha_rise=0.5, alpha_decay=0.5)
    for _ in range(100):
        f.update(0.7)
    assert abs(f.value - 0.7) < 1e-6


def test_vector_form_is_elementwise_asymmetric():
    f = ExpFilter(np.zeros(3), alpha_rise=0.8, alpha_decay=0.2)
    out = f.update(np.array([1.0, 0.0, 1.0]))
    assert np.allclose(out, [0.8, 0.0, 0.8])
    # Bin 0 falls (slow decay), bins 1/2 rise (fast): asymmetry is per element.
    out = f.update(np.array([0.0, 1.0, 1.0]))
    assert np.allclose(out, [0.8 * 0.8, 0.8, 0.8 * 1.0 + 0.2 * 0.8])


def test_reset_forgets_history():
    f = ExpFilter(0.0, alpha_rise=0.5, alpha_decay=0.5)
    f.update(1.0)
    f.reset(0.0)
    assert f.value == 0.0
