"""The rhythm-confidence gate: club modes must not strobe beat-less music.

Intense/Extreme deliberately keep permissive onset gates (width_min 0.08/0.05
— "all but the purest tones"), so on music with no actual beat (pads, vocals,
ambience) false onsets used to hammer a dark room at the relaxed limiter's
budget. The engine now tracks a rhythm-confidence envelope — proof the song
HAS a beat (a locked tempo grid, or clearly broadband kicks while unlocked) —
and scales the unlocked detected-flash path between the mode's
``nobeat_flash`` floor and full. Real-beat music is untouched: the locked
scheduled path never passes through the gate.
"""

from __future__ import annotations

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS
from hue_music_sync.hue.bridge import EntertainmentChannel

_DT = 0.02  # 50 fps, matching the analysis hop


def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(
            channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.5
        )
        for i in range(n)
    ]


def _frame(
    *,
    bass_beat: bool = False,
    onset_width: float = 1.0,
    energy: float = 0.6,
) -> AnalysisFrame:
    """A playing-music frame loud enough to keep the silence gate open."""
    return AnalysisFrame(
        bands={"sub_bass": 0.7, "bass": 0.8, "low_mid": 0.4, "mid": 0.3, "high": 0.2},
        energy=energy,
        bass_beat=bass_beat,
        bass_strength=2.0 if bass_beat else 0.0,
        # Onset flux present so a real / scheduled beat clears Extreme's flux gate.
        bass_flux=1.0,
        melbank=[0.3] * 16,
        salience=1.0,
        onset_width=onset_width,
    )


def _locked_grid() -> BeatGrid:
    return BeatGrid(bpm=120.0, confidence=0.8, locked=True, period_s=0.5)


def _engine(mode: SyncMode) -> EffectEngine:
    eng = EffectEngine(_channels())
    eng.set_mode(mode)
    return eng


def _run(eng: EffectEngine, frames, grids=None) -> float:
    """Render frames; return the biggest per-light flash overlay seen."""
    peak = 0.0
    for i, f in enumerate(frames):
        grid = grids[i] if grids is not None else None
        eng.render(f, _DT, beatgrid=grid)
        if eng._light_flash:
            peak = max(peak, max(eng._light_flash.values()))
    return peak


# --- the confidence envelope itself -------------------------------------------


def test_conf_stays_low_on_narrow_onsets():
    # Vowel-like onsets (width ~0.12, below the kick evidence span) firing
    # repeatedly with no tempo lock: confidence must not build.
    eng = _engine(SyncMode.EXTREME)
    for i in range(300):
        beat = i % 25 == 0  # an irregular-ish stream of false "beats"
        eng.render(_frame(bass_beat=beat, onset_width=0.12), _DT)
    assert eng._rhythm_conf < 0.05


def test_conf_rises_fast_while_locked():
    eng = _engine(SyncMode.EXTREME)
    grid = _locked_grid()
    for _ in range(50):  # one second of locked grid
        eng.render(_frame(), _DT, beatgrid=grid)
    assert eng._rhythm_conf > 0.8


def test_conf_recovers_within_a_few_real_kicks():
    # An unlocked intro with real (broadband) kicks: a handful of hits must
    # restore most of the confidence, so real music never feels muted.
    eng = _engine(SyncMode.EXTREME)
    for i in range(100):
        kick = i % 25 == 0  # 4 kicks over 2 s
        eng.render(_frame(bass_beat=kick, onset_width=0.55), _DT)
    assert eng._rhythm_conf > 0.6


def test_conf_decays_when_the_beat_stops():
    eng = _engine(SyncMode.EXTREME)
    grid = _locked_grid()
    for _ in range(100):
        eng.render(_frame(), _DT, beatgrid=grid)
    for _ in range(400):  # 8 s of beat-less music
        eng.render(_frame(), _DT)
    assert eng._rhythm_conf < 0.2


# --- the gate on the unlocked flash path --------------------------------------


def test_extreme_rejects_narrow_noise_but_fires_on_real_kicks():
    # The user's 'stop flashing on noise/vocals': narrow, tonal/vocal-like onsets
    # with no rhythm evidence must NOT flash Extreme — the width gate, the raised
    # beat threshold and the low nobeat floor reject them outright — while a
    # broadband kick still slams.
    narrow = [_frame(bass_beat=(i % 25 == 0), onset_width=0.12) for i in range(100)]
    assert _run(_engine(SyncMode.EXTREME), narrow) <= 0.05  # noise/vocals: rejected

    real = [_frame(bass_beat=(i % 25 == 0), onset_width=0.55) for i in range(100)]
    assert _run(_engine(SyncMode.EXTREME), real) > 0.4  # broadband kicks fire hard


def test_locked_scheduled_path_is_untouched():
    # A locked grid's scheduled beats never pass through the gate: two engines
    # with opposite confidence histories flash identically on the same
    # scheduled beat.
    beat = _frame()
    grid = _locked_grid()
    grid.predicted_beat = True
    grid.accent_now = 1.0

    fresh = _engine(SyncMode.EXTREME)  # zero confidence
    fresh.render(beat, _DT, beatgrid=grid)
    fresh_peak = max(fresh._light_flash.values())

    charged = _engine(SyncMode.EXTREME)
    steady = _locked_grid()
    for _ in range(150):
        charged.render(_frame(), _DT, beatgrid=steady)
    charged._light_flash.clear()
    charged.render(beat, _DT, beatgrid=grid)
    charged_peak = max(charged._light_flash.values())

    assert abs(fresh_peak - charged_peak) < 1e-6


def test_strict_modes_unaffected():
    # High keeps nobeat_flash == 1.0: the gate is a provable no-op there.
    assert MODE_PARAMS[SyncMode.HIGH].nobeat_flash == 1.0
    assert MODE_PARAMS[SyncMode.MEDIUM].nobeat_flash == 1.0
    onsets = [_frame(bass_beat=(i % 25 == 0), onset_width=0.12) for i in range(100)]
    cold = _run(_engine(SyncMode.HIGH), onsets)

    warm_eng = _engine(SyncMode.HIGH)
    grid = _locked_grid()
    for _ in range(150):
        warm_eng.render(_frame(), _DT, beatgrid=grid)
    warm_eng._light_flash.clear()
    warm = _run(warm_eng, onsets)
    assert abs(cold - warm) < 1e-6
