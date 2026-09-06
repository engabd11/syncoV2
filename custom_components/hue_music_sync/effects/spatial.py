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


def floor_origin(
    positions: dict[int, tuple[float, float, float]],
    configuration_type: str = "room",
) -> tuple[float, float, float]:
    """A sensible wave origin: horizontally central, at floor height.

    For ``room``-type areas (the default) the origin is the room centre at
    floor level — a bass thump reads best rising from the centre/low part and
    expanding outward and upward.

    For ``screen``-type areas the lamp positions are relative to a screen and
    the user, so the origin shifts to the front centre (where the screen is)
    to make effects emanate from the screen direction.
    """
    if not positions:
        return (0.5, 0.5, 0.0)
    n = len(positions)
    mx = sum(p[0] for p in positions.values()) / n
    my = sum(p[1] for p in positions.values()) / n
    mz = min(p[2] for p in positions.values())
    if configuration_type == "screen":
        # Screen areas: waves emanate from the front centre (y near 1.0 in
        # normalised coords = closest to the screen/user).
        return (mx, max(my, 0.8), mz)
    return (mx, my, mz)


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def phrase_origins(
    positions: dict[int, tuple[float, float, float]],
    configuration_type: str = "room",
) -> list[tuple[float, float, float]]:
    """Deterministic wave-origin cycle for phrase-level variation.

    Centre → left → right → centre, all at floor height, so waves sweep the
    room from a different corner each musical phrase and the classic centred
    bloom recurs every other phrase. Deterministic (no seed) so two runs of
    the same song render identically. For screen-type areas the origins
    shift toward the front (screen) edge.
    """
    centre = floor_origin(positions, configuration_type)
    if not positions:
        return [centre]
    my = sum(p[1] for p in positions.values()) / len(positions)
    mz = min(p[2] for p in positions.values())
    if configuration_type == "screen":
        my = max(my, 0.8)
    return [centre, (0.15, my, mz), (0.85, my, mz), centre]


@dataclass(slots=True)
class Wave:
    """An expanding spherical pulse launched on a beat."""

    origin: tuple[float, float, float]
    strength: float
    speed: float  # normalised units per second
    width: float  # thickness of the wavefront shell
    age: float = 0.0
    # Which phrase origin this wave launched from (indexes the per-channel
    # precomputed distances, so amplitude_at needs no per-frame sqrt).
    origin_idx: int = 0

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


# --- Colour tilt + spatial coupling (P4, ported from CAMusic) ---------------

# Dominantly left-right tilted room axis for the colour field (see
# :func:`colour_axis_projection`).
COLOUR_AXIS_X = 0.845
COLOUR_AXIS_Y = 0.296
COLOUR_AXIS_Z = 0.465

# ``exp(-x^2) = 0.6`` at ``x ~= 0.715``, so dividing the mean
# nearest-neighbour distance by this puts a lamp's neighbour at weight ~0.6 —
# close enough to bind them, far from making the room a single average.
SIGMA_NEIGHBOUR_SCALE = 0.715
# Bounds on sigma as a fraction of the unit room cube.
SIGMA_MIN = 0.15
SIGMA_MAX = 0.60


def colour_axis_projection(pos: tuple[float, float, float]) -> float:
    """Project a position onto a tilted room axis, for colour.

    Colour has always been a function of the x axis alone — ``xrank`` — so a
    room's hue could only ever sweep left to right, and two lamps at the
    same x but different heights or depths were always the same colour
    however far apart they were. This gives the field second and third
    dimensions to drift in.

    Dominantly left-right, so the result still reads as the same effect
    rather than a new one. An axis with no spread collapses to 0.5 in
    :func:`normalize_positions` and contributes a constant, which the
    caller's min-max normalisation then removes entirely — so a flat room
    renders exactly as it did before.
    """
    return (
        COLOUR_AXIS_X * pos[0]
        + COLOUR_AXIS_Y * pos[1]
        + COLOUR_AXIS_Z * pos[2]
    )


def coupling_kernel(
    positions: list[tuple[float, float, float]],
) -> list[list[float]]:
    """Row-stochastic Gaussian kernel over lamp positions.

    The thing that makes a room read as one field rather than as N
    independent visualisers: row ``i`` says how much lamp ``i`` should hear
    of each other lamp.

    **Rows sum to 1** — the property that matters. A row-stochastic matrix
    applied to a set of values is a weighted average, so the room's total
    energy is preserved and the coupling can neither brighten nor dim it,
    only redistribute. It also means a *constant* field comes back
    unchanged, so coupling has no effect at all on a moment when every lamp
    already agrees.

    sigma is derived from the area's own geometry — the mean
    nearest-neighbour distance — so a tight cluster of four bulbs and a
    room spanning fifteen metres both get a neighbourhood that means the
    same thing relative to their own spacing.

    Computed once, at construction: the shape never changes, only how much
    of it is mixed in. Ported from CAMusic's
    ``SpatialWaves.couplingKernel``.
    """
    n = len(positions)
    if n <= 1:
        return [[1.0]]
    near_sum = 0.0
    near_count = 0
    for i in range(n):
        best = float("inf")
        for j in range(n):
            if i == j:
                continue
            d = distance(positions[i], positions[j])
            if d < best:
                best = d
        if best < float("inf"):
            near_sum += best
            near_count += 1
    mean_near = near_sum / near_count if near_count else 0.3
    sigma = min(SIGMA_MAX, max(SIGMA_MIN, mean_near / SIGMA_NEIGHBOUR_SCALE))

    rows: list[list[float]] = []
    for i in range(n):
        row = []
        for j in range(n):
            d = distance(positions[i], positions[j]) / sigma
            row.append(math.exp(-d * d))
        total = sum(row)  # includes the self term (always 1): never zero
        rows.append([w / total for w in row])
    return rows


# --- Colour tilt + spatial coupling (P4, ported from CAMusic) ---------------

# Dominantly left-right tilted room axis for the colour field (see
# :func:`colour_axis_projection`).
COLOUR_AXIS_X = 0.845
COLOUR_AXIS_Y = 0.296
COLOUR_AXIS_Z = 0.465

# ``exp(-x^2) = 0.6`` at ``x ~= 0.715``, so dividing the mean
# nearest-neighbour distance by this puts a lamp's neighbour at weight ~0.6 —
# close enough to bind them, far from making the room a single average.
SIGMA_NEIGHBOUR_SCALE = 0.715
# Bounds on sigma as a fraction of the unit room cube.
SIGMA_MIN = 0.15
SIGMA_MAX = 0.60


def colour_axis_projection(pos: tuple[float, float, float]) -> float:
    """Project a position onto a tilted room axis, for colour.

    Colour has always been a function of the x axis alone — ``xrank`` — so a
    room's hue could only ever sweep left to right, and two lamps at the
    same x but different heights or depths were always the same colour
    however far apart they were. This gives the field second and third
    dimensions to drift in.

    Dominantly left-right, so the result still reads as the same effect
    rather than a new one. An axis with no spread collapses to 0.5 in
    :func:`normalize_positions` and contributes a constant, which the
    caller's min-max normalisation then removes entirely — so a flat room
    renders exactly as it did before.
    """
    return (
        COLOUR_AXIS_X * pos[0]
        + COLOUR_AXIS_Y * pos[1]
        + COLOUR_AXIS_Z * pos[2]
    )


def coupling_kernel(
    positions: list[tuple[float, float, float]],
) -> list[list[float]]:
    """Row-stochastic Gaussian kernel over lamp positions.

    The thing that makes a room read as one field rather than as N
    independent visualisers: row ``i`` says how much lamp ``i`` should hear
    of each other lamp.

    **Rows sum to 1** — the property that matters. A row-stochastic matrix
    applied to a set of values is a weighted average, so the room's total
    energy is preserved and the coupling can neither brighten nor dim it,
    only redistribute. It also means a *constant* field comes back
    unchanged, so coupling has no effect at all on a moment when every lamp
    already agrees.

    sigma is derived from the area's own geometry — the mean
    nearest-neighbour distance — so a tight cluster of four bulbs and a
    room spanning fifteen metres both get a neighbourhood that means the
    same thing relative to their own spacing.

    Computed once, at construction: the shape never changes, only how much
    of it is mixed in. Ported from CAMusic's
    ``SpatialWaves.couplingKernel``.
    """
    n = len(positions)
    if n <= 1:
        return [[1.0]]
    near_sum = 0.0
    near_count = 0
    for i in range(n):
        best = float("inf")
        for j in range(n):
            if i == j:
                continue
            d = distance(positions[i], positions[j])
            if d < best:
                best = d
        if best < float("inf"):
            near_sum += best
            near_count += 1
    mean_near = near_sum / near_count if near_count else 0.3
    sigma = min(SIGMA_MAX, max(SIGMA_MIN, mean_near / SIGMA_NEIGHBOUR_SCALE))

    rows: list[list[float]] = []
    for i in range(n):
        row = []
        for j in range(n):
            d = distance(positions[i], positions[j]) / sigma
            row.append(math.exp(-d * d))
        total = sum(row)  # includes the self term (always 1): never zero
        rows.append([w / total for w in row])
    return rows


def height_band(nz: float) -> str:
    """Map a lamp's height to the frequency band it should favour.

    Bass on the floor, treble at the ceiling — the natural way a room's energy
    stacks. Continuous blending between bands is done by the caller.
    """
    bands = ("sub_bass", "bass", "low_mid", "mid", "high")
    idx = min(len(bands) - 1, int(nz * len(bands)))
    return bands[idx]
