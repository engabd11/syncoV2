"""3D spatial helpers — the thing LED strips physically cannot do.

A Hue entertainment area is real lamps at real positions in a room: every channel
carries an ``(x, y, z)`` (left↔right, back↔front, floor↔ceiling). Strip-based
sync (LedFx/WLED) is one-dimensional; here we sample a *field* in 3D so a kick can
send a **wavefront sweeping across the room**, treble can live up high and bass
down low, and colour can drift in two dimensions instead of just left-to-right.

Pure and dependency-free (a little math only) so the geometry is unit-tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def normalize_positions(channels) -> dict[int, tuple[float, float, float]]:
    """Map each channel's (x,y,z) to 0..1 over the area's actual extent.

    Normalising to the real spread (not the nominal [-1,1] cube) keeps effects
    well-scaled whether the lamps fill the room or cluster in one corner. A axis
    with no spread (e.g. all lamps level, or a 1D/collinear area) collapses to
    0.5 so effects on that axis simply do nothing rather than divide by zero.
    """
    if not channels:
        return {}
    xs = [c.x for c in channels]
    ys = [c.y for c in channels]
    zs = [c.z for c in channels]

    def scaler(vals):
        lo, hi = min(vals), max(vals)
        span = hi - lo
        if span < 1e-6:
            return lambda v: 0.5
        return lambda v: (v - lo) / span

    sx, sy, sz = scaler(xs), scaler(ys), scaler(zs)
    return {c.channel_id: (sx(c.x), sy(c.y), sz(c.z)) for c in channels}


def floor_origin(positions: dict[int, tuple[float, float, float]]) -> tuple[float, float, float]:
    """A sensible wave origin: horizontally central, at floor height.

    A bass thump reads best rising from the centre/low part of the room and
    expanding outward and upward.
    """
    if not positions:
        return (0.5, 0.5, 0.0)
    n = len(positions)
    mx = sum(p[0] for p in positions.values()) / n
    my = sum(p[1] for p in positions.values()) / n
    mz = min(p[2] for p in positions.values())
    return (mx, my, mz)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


@dataclass(slots=True)
class Wave:
    """An expanding spherical pulse launched on a beat."""

    origin: tuple[float, float, float]
    strength: float
    speed: float  # normalised units per second
    width: float  # thickness of the wavefront shell
    age: float = 0.0

    def advance(self, dt: float, decay_tau: float) -> None:
        self.age += dt
        self.strength *= math.exp(-dt / decay_tau)

    @property
    def radius(self) -> float:
        return self.age * self.speed

    def amplitude_at(self, d: float) -> float:
        """Brightness this wave contributes to a point ``d`` from the origin."""
        shell = (d - self.radius) / self.width
        return self.strength * math.exp(-shell * shell)

    def dead(self, max_distance: float = 1.8) -> bool:
        return self.strength < 0.02 or self.radius > max_distance + 3.0 * self.width


def melbank_window(spectral_pos: float, n_bins: int, span: float = 0.20) -> tuple[int, int]:
    """Half-open bin range ``[lo, hi)`` a lamp should average from the melbank.

    Maps the room's spatial axis to the spectrum the way LedFx's "Wavelength"
    spreads a melbank along a strip: a lamp at ``spectral_pos`` 0 rides the
    lowest frequencies, one at 1 the highest. Each lamp averages a window
    (``span`` of the bins, min one bin) so neighbours overlap into a smooth
    field instead of hard-edged bands. Empty range only if there are no bins.
    """
    if n_bins <= 0:
        return 0, 0
    pos = 0.0 if spectral_pos < 0.0 else 1.0 if spectral_pos > 1.0 else spectral_pos
    center = pos * (n_bins - 1)
    half = max(0.5, span * n_bins)
    lo = int(math.floor(center - half))
    hi = int(math.ceil(center + half)) + 1
    lo = max(0, lo)
    hi = min(n_bins, hi)
    return lo, max(lo + 1, hi)


def height_band(nz: float) -> str:
    """Map a lamp's height to the frequency band it should favour.

    Bass on the floor, treble at the ceiling — the natural way a room's energy
    stacks. Continuous blending between bands is done by the caller.
    """
    bands = ("sub_bass", "bass", "low_mid", "mid", "high")
    idx = min(len(bands) - 1, int(nz * len(bands)))
    return bands[idx]
