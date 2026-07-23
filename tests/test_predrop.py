"""Pre-drop choreography: the room pulls in before a drop, then detonates.

The track map tells the engine a clearly-louder section boundary is coming
(``structure.drop_imminent`` + ``drop_eta_s``); the engine answers with the
classic pro-lighting move — dim toward the floor, desaturate, hold back the
pulse — and snap-releases the moment the drop lands, with the drop swell
sized by the depth of the held tension. The heuristic (map-less) flag is
treated with suspicion: confirm, cap, timeout, refractory.
"""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.structure import StructureState
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02


def _channels(n: int = 4) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(
            channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.5
        )
        for i in range(n)
    ]


def _frame() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.6, "bass": 0.7, "low_mid": 0.5, "mid": 0.4, "high": 0.3},
        energy=0.6,
        melbank=[0.5] * 16,
        salience=1.0,
    )


def _engine(mode: SyncMode) -> EffectEngine:
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    return eng


def _room(out) -> float:
    return max(max(c) for c in out.values())


def _run(eng: EffectEngine, states, collect=False):
    levels = []
    for st in states:
        out = eng.render(_frame(), _DT, beatgrid=None, structure=st)
        if collect:
            levels.append(_room(out))
    return levels


def _steady(n: int) -> list[StructureState]:
    return [StructureState() for _ in range(n)]


def _scheduled_countdown(n: int, window_s: float = 2.0) -> list[StructureState]:
    """A map-scheduled drop approaching: eta counts down over ``n`` frames."""
    states = []
    for i in range(n):
        eta = window_s * (1.0 - i / n)
        states.append(StructureState(drop_imminent=True, drop_eta_s=eta))
    return states


def test_scheduled_predrop_dims_then_detonates():
    eng = _engine(SyncMode.INTENSE)
    warm = _run(eng, _steady(150), collect=True)  # settle the continuous layer
    base = sum(warm[-25:]) / 25

    dip = _run(eng, _scheduled_countdown(100), collect=True)  # 2 s countdown
    held = min(dip[-25:])  # deepest pull-down in the final half second
    assert held < base * 0.6  # the room clearly pulls in (INTENSE depth 0.60)

    # The detonation is a slew-limited fast SWING (by design, never a 1-frame
    # strobe): measure its peak over the following ~0.2 s.
    after = [StructureState(drop_now=True)] + _steady(10)
    boom = max(_run(eng, after, collect=True))
    assert boom > held + 0.3  # the detonation punches out of the held dim
    assert eng._predrop == 0.0  # snap release, not an ease


def test_drop_swell_bigger_after_held_predrop():
    # The same drop lands bigger when it detonates out of a held pull-down.
    cold = _engine(SyncMode.INTENSE)
    _run(cold, _steady(150))
    cold.render(_frame(), _DT, structure=StructureState(drop_now=True))
    cold_swell = cold._swell

    held = _engine(SyncMode.INTENSE)
    _run(held, _steady(150))
    _run(held, _scheduled_countdown(100))
    held.render(_frame(), _DT, structure=StructureState(drop_now=True))
    assert held._swell > cold_swell * 1.15


def test_subtle_is_provably_inert():
    # SUBTLE has predrop_depth == 0 and base == floor: the pull-down must not
    # move it at all.
    eng = _engine(SyncMode.SUBTLE)
    warm = _run(eng, _steady(150), collect=True)
    dip = _run(eng, _scheduled_countdown(100), collect=True)
    assert abs(min(dip) - warm[-1]) < 0.02


def test_heuristic_flicker_never_commits():
    # drop_imminent flapping on/off (a jittery build detector) must never
    # reach the confirm streak, so the envelope stays at zero.
    eng = _engine(SyncMode.INTENSE)
    _run(eng, _steady(150))
    states = [
        StructureState(drop_imminent=(i % 3 == 0))  # never 10 in a row
        for i in range(200)
    ]
    _run(eng, states)
    assert eng._predrop == 0.0
    assert not eng._predrop_commit


def test_heuristic_timeout_releases_and_blocks_recommit():
    eng = _engine(SyncMode.INTENSE)
    _run(eng, _steady(150))
    # A persistent heuristic flag commits after the confirm streak...
    _run(eng, [StructureState(drop_imminent=True) for _ in range(50)])
    assert eng._predrop_commit
    assert eng._predrop > 0.3
    # ...but with no drop it times out, eases back, and blocks re-commits.
    _run(eng, [StructureState(drop_imminent=True) for _ in range(200)])  # 4 s
    assert not eng._predrop_commit
    assert eng._predrop == 0.0
    _run(eng, [StructureState(drop_imminent=True) for _ in range(50)])  # 1 s later
    assert not eng._predrop_commit  # still inside the refractory


def test_scheduled_path_bypasses_confirm_and_refractory():
    # The map is trusted: even during a heuristic refractory, a scheduled ETA
    # commits immediately.
    eng = _engine(SyncMode.INTENSE)
    _run(eng, _steady(150))
    _run(eng, [StructureState(drop_imminent=True) for _ in range(250)])  # timeout
    assert eng._predrop_block_until > eng.time
    _run(eng, _scheduled_countdown(50))
    assert eng._predrop_commit
    assert eng._predrop > 0.3
