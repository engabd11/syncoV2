"""Asymmetric exponential smoothing — the LedFx ``ExpFilter`` primitive.

LedFx's whole "always alive" feel comes from one small building block: an
exponential moving average with *different* time constants for rising vs falling
values. Energy snaps up fast (``alpha_rise`` near 1) and eases down gently
(``alpha_decay`` small), so a melbank or an effect output tracks transients
without flickering and decays smoothly into the gaps between them.

Pure and dependency-light (numpy only) so it can be shared by the analyzer
(melbank smoothing) and unit-tested in isolation. Works on a scalar or on a
numpy vector (per-bin smoothing) — the value's shape is whatever it is first
seeded with.
"""

from __future__ import annotations

import numpy as np


class ExpFilter:
    """Exponentially-weighted smoother with asymmetric rise/decay.

    ``alpha`` values are in ``(0, 1]``; higher reacts faster. With
    ``alpha_rise > alpha_decay`` the output jumps toward rising input and lags
    behind falling input — the LedFx default that makes band power read as a
    quick attack and a slow release.
    """

    __slots__ = ("alpha_rise", "alpha_decay", "value")

    def __init__(
        self,
        value: float | np.ndarray = 0.0,
        alpha_rise: float = 0.5,
        alpha_decay: float = 0.5,
    ) -> None:
        self.alpha_rise = float(alpha_rise)
        self.alpha_decay = float(alpha_decay)
        self.value = value

    def update(self, value: float | np.ndarray) -> float | np.ndarray:
        """Fold one new sample in and return the smoothed value."""
        if isinstance(self.value, np.ndarray) or isinstance(value, np.ndarray):
            alpha = np.where(value > self.value, self.alpha_rise, self.alpha_decay)
        else:
            alpha = self.alpha_rise if value > self.value else self.alpha_decay
        self.value = alpha * value + (1.0 - alpha) * self.value
        return self.value

    def reset(self, value: float | np.ndarray = 0.0) -> None:
        """Forget history and reseed (e.g. on a track change)."""
        self.value = value
