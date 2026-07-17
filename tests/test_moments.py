"""Scheduled "moments" (v5 maps): stop-gaps, ranked drops, risers, fills, the
outro fade, chorus recall — and how the engine choreographs each of them.

These are the events a live detector can only chase; the offline analysis
knows them ahead of time, which is what makes the lights feel like part of
the song instead of a reaction to it.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.audio.structure import StructureState
from hue_music_sync.audio.trackmap import (
    MOMENT_DROP,
    MOMENT_FADE,
    MOMENT_GAP,
    MOMENT_RISER,
    Section,
    TrackMap,
    _detect_moments,
    _group_sections,
    analyze_pcm,
)
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import ROLE_VOCAL
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_FP = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE
_DT = _FP


def _frames(seconds: float) -> int:
    return int(round(seconds / _FP))


def _detect(rms_k, *, bass_env=None, centroid=None, sections=None,
            beats=None, onsets=None):
    n = rms_k.size
    return _detect_moments(
        bass_env if bass_env is not None else np.zeros(n),
        rms_k,
        centroid if centroid is not None else np.full(n, 0.5),
        sections if sections is not None else [Section(0.0, n * _FP, 0.8)],
        beats if beats is not None else np.zeros(0),
        onsets if onsets is not None else np.zeros(0),
        n * _FP,
    )


# --- detection -----------------------------------------------------------------

def test_stop_gap_detected_between_loud_passages():
    n = _frames(30.0)
    rms = np.full(n, 0.5)
    g0, g1 = _frames(15.0), _frames(15.4)
    rms[g0:g1] = 0.0005
    kinds, t0, t1, s = _detect(rms)
    gaps = np.flatnonzero(kinds == MOMENT_GAP)
    assert gaps.size == 1
    assert abs(t0[gaps[0]] - 15.0) < 0.3 and abs(t1[gaps[0]] - 15.4) < 0.3
    assert s[gaps[0]] > 0.7  # re-entry is loud → a full detonation


def test_gap_not_detected_at_track_edges_or_in_quiet_context():
    n = _frames(30.0)
    rms = np.full(n, 0.5)
    rms[: _frames(0.4)] = 0.0005  # lead-in silence is not a cut
    kinds, *_ = _detect(rms)
    assert not np.any(kinds == MOMENT_GAP)


def test_drop_ranked_and_riser_spans_the_build():
    n = _frames(60.0)
    rms = np.full(n, 0.18)
    # A build from 40..50 s (loudness + brightness climb), then the drop.
    b0, b1 = _frames(40.0), _frames(50.0)
    rms[b0:b1] = np.linspace(0.18, 0.30, b1 - b0)
    rms[b1:] = 0.55
    centroid = np.full(n, 0.30)
    centroid[b0:b1] = np.linspace(0.30, 0.46, b1 - b0)
    centroid[b1:] = 0.40
    bass = np.full(n, 0.2)
    bass[b1:] = 1.0
    sections = [Section(0.0, 50.0, 0.35), Section(50.0, 60.0, 0.95)]
    kinds, t0, t1, s = _detect(
        rms, bass_env=bass, centroid=centroid, sections=sections
    )
    drops = np.flatnonzero(kinds == MOMENT_DROP)
    assert drops.size == 1
    assert abs(t0[drops[0]] - 50.0) < 0.1
    assert s[drops[0]] == 1.0  # the track's biggest drop detonates at full
    risers = np.flatnonzero(kinds == MOMENT_RISER)
    assert risers.size == 1
    assert t1[risers[0]] == t0[drops[0]]  # the riser resolves AT the drop
    assert t0[drops[0]] - t0[risers[0]] >= 6.0  # the whole build, not 1.2 s


def test_outro_fade_detected():
    n = _frames(60.0)
    rms = np.full(n, 0.5)
    f0 = _frames(45.0)
    rms[f0:] = np.linspace(0.5, 0.005, n - f0)
    kinds, t0, t1, _s = _detect(rms)
    fades = np.flatnonzero(kinds == MOMENT_FADE)
    assert fades.size == 1
    assert 45.0 <= t0[fades[0]] <= 55.0
    assert abs(t1[fades[0]] - 60.0) < 1e-6


# --- moment_state playback accessor ---------------------------------------------

def _map_with_moments() -> TrackMap:
    return TrackMap(
        duration=60.0, bpm=120.0, confidence=0.5,
        beats=np.arange(0.0, 60.0, 0.5), accents=np.ones(120), downbeat=0,
        mom_kind=np.array([MOMENT_GAP, MOMENT_RISER, MOMENT_FADE], dtype=np.int8),
        mom_t0=np.array([20.0, 30.0, 50.0]),
        mom_t1=np.array([20.5, 38.0, 60.0]),
        mom_strength=np.array([0.9, 1.0, 1.0], dtype=np.float32),
    )


def test_moment_state_reports_gap_riser_fade():
    tm = _map_with_moments()
    assert tm.moment_state(20.2).get("gap") is True
    rel = tm.moment_state(20.6, prev_pos=20.4)
    assert rel.get("gap_release") == np.float32(0.9)
    ris = tm.moment_state(34.0)
    assert abs(ris["riser"] - 0.5) < 0.01
    assert abs(ris["riser_eta"] - 4.0) < 0.01
    assert abs(tm.moment_state(55.0)["fade"] - 0.5) < 0.01
    assert tm.moment_state(5.0) == {}


def test_moments_survive_save_and_load(tmp_path):
    tm = _map_with_moments()
    tm.sections = [Section(0.0, 30.0, 0.4, group=0), Section(30.0, 60.0, 0.9, group=1)]
    path = tmp_path / "m.npz"
    tm.save(path)
    back = TrackMap.load(path)
    assert back is not None
    np.testing.assert_array_equal(back.mom_kind, tm.mom_kind)
    np.testing.assert_allclose(back.mom_t0, tm.mom_t0)
    np.testing.assert_allclose(back.mom_strength, tm.mom_strength)
    assert [s.group for s in back.sections] == [0, 1]


# --- chorus recall ---------------------------------------------------------------

def test_sections_group_by_similarity_and_chorus_is_the_loud_recurrer():
    # A-B-A-B structure: verses share a spectral shape, choruses share another.
    n_blocks = 40
    dim = 8
    feat = np.zeros((n_blocks, dim))
    pat_a = np.array([1.0, 0.8, 0.2, 0.1, 0.0, 0.0, 0.0, 0.0])
    pat_b = np.array([0.1, 0.2, 0.3, 0.9, 1.0, 0.8, 0.5, 0.3])
    for i in range(n_blocks):
        feat[i] = pat_a if (i // 10) % 2 == 0 else pat_b
    sections = [
        Section(0.0, 7.5, 0.4), Section(7.5, 15.0, 0.9),
        Section(15.0, 22.5, 0.42), Section(22.5, 30.0, 0.92),
    ]
    _group_sections(sections, feat, n_blocks)
    assert sections[0].group == sections[2].group  # verse == verse
    assert sections[1].group == sections[3].group  # chorus == chorus
    assert sections[0].group != sections[1].group
    tm = TrackMap(duration=30.0, bpm=120.0, confidence=0.5,
                  beats=np.zeros(0), accents=np.zeros(0), downbeat=0,
                  sections=sections)
    assert tm.chorus_group == sections[1].group  # the loud recurring group


def test_engine_section_identity_is_stable_per_group():
    eng = EffectEngine(_channels())
    eng.set_section_identity(2, chorus=True)
    off = eng.section_offset
    assert off > 0.0 and eng.chorus_now
    eng.set_section_identity(0, chorus=False)
    eng.set_section_identity(2, chorus=True)
    assert eng.section_offset == off  # the same group looks the same, always
    eng.set_section_identity(-1, False)
    assert eng.section_offset == 0.0 and not eng.chorus_now


# --- engine choreography of the moments ------------------------------------------

def _channels(n: int = 4) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.5)
        for i in range(n)
    ]


def _loud_frame() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.7, "bass": 0.8, "low_mid": 0.5, "mid": 0.5, "high": 0.4},
        energy=0.7,
        melbank=[0.6] * 16,
        salience=1.0,
    )


def _room(out) -> float:
    return max(max(c) for c in out.values())


def test_gap_snaps_the_room_to_the_floor_instantly():
    eng = EffectEngine(_channels())
    eng.set_mode(SyncMode.HIGH)
    for _ in range(100):
        bright = _room(eng.render(_loud_frame(), _DT, structure=StructureState()))
    assert bright > 0.3
    cut = _room(eng.render(_loud_frame(), _DT, structure=StructureState(gap_now=True)))
    assert cut < 0.12  # one frame: crisp, not eased through bri_decay


def test_ranked_drop_strength_sizes_the_detonation():
    def boom(strength: float) -> float:
        eng = EffectEngine(_channels())
        eng.set_mode(SyncMode.EXTREME)
        for _ in range(60):
            eng.render(_loud_frame(), _DT, structure=StructureState())
        peak = 0.0
        states = [StructureState(drop_now=True, drop_strength=strength)] + [
            StructureState() for _ in range(8)
        ]
        for st in states:
            peak = max(peak, _room(eng.render(_loud_frame(), _DT, structure=st)))
        return peak

    assert boom(1.0) > boom(0.4) + 0.05


def test_fill_pulls_every_role_into_the_beat():
    from hue_music_sync.audio.tempo import BeatGrid

    def vocal_level(fill: bool) -> float:
        # An ordinary (non-full-room) beat: accent below HIGH's
        # full_room_accent, so only the fill can pull the vocal lamp in.
        eng = EffectEngine(_channels(6))
        eng.set_mode(SyncMode.HIGH)
        st = StructureState(fill_now=fill)
        grid = BeatGrid(bpm=120.0, confidence=0.9, locked=True, period_s=0.5,
                        phase=0.0, time_to_next_beat=0.5, next_beat_t=0.0,
                        bar_phase=0.0, predicted_beat=False, accent=0.6,
                        accent_now=0.6, beat_in_bar=1)
        for _ in range(30):
            eng.render(_loud_frame(), _DT, beatgrid=grid, structure=st)
        vocal = next(c for c, r in eng.roles.items() if r == ROLE_VOCAL)
        beat = BeatGrid(bpm=120.0, confidence=0.9, locked=True, period_s=0.5,
                        phase=0.0, time_to_next_beat=0.0, next_beat_t=0.0,
                        bar_phase=0.0, predicted_beat=True, accent=0.6,
                        accent_now=0.6, beat_in_bar=1)
        eng.render(_loud_frame(), _DT, beatgrid=beat, structure=st)
        return eng._light_flash.get(vocal, 0.0)

    assert vocal_level(True) > vocal_level(False) + 0.2


# --- end-to-end: a real analysed track carries its moments ------------------------

def test_analyze_pcm_finds_a_gap_in_real_audio():
    seconds = 45.0
    n = int(_SR * seconds)
    t = np.arange(n) / _SR
    rng = np.random.default_rng(7)
    sig = (rng.standard_normal(n) * 0.2).astype(np.float32)
    env = np.exp(-np.arange(int(0.12 * _SR)) / (0.035 * _SR))
    kick = (np.sin(2 * np.pi * 55 * np.arange(len(env)) / _SR) * env).astype(np.float32)
    for bt in np.arange(0.3, seconds, 0.5):
        i = int(bt * _SR)
        seg = kick[: n - i]
        sig[i : i + len(seg)] += seg
    g0, g1 = int(20.0 * _SR), int(20.5 * _SR)
    sig[g0:g1] = 0.0
    peak = float(np.max(np.abs(sig)))
    tm = analyze_pcm((sig / peak * 0.9).astype(np.float32))
    assert tm is not None
    gaps = np.flatnonzero(tm.mom_kind == MOMENT_GAP)
    assert gaps.size >= 1
    assert any(abs(tm.mom_t0[g] - 20.0) < 0.5 for g in gaps)
