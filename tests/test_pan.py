"""Stereo pan mapping: panned instruments light the matching side of the room.

The load-bearing invariant is MONO PARITY: every mono feature comes from the
mid (L+R)/2 — bit-identical to the old ``-ac 1`` downmix — so all existing
thresholds, AGCs and mode tunings hold, and ``pan`` is purely additive.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer, AnalysisFrame
from hue_music_sync.audio.trackmap import TrackFeatures, TrackMap
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, MELBANK_BINS, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_DT = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE


def _music(seconds: float = 4.0) -> np.ndarray:
    rng = np.random.default_rng(11)
    n = int(_SR * seconds)
    t = np.arange(n) / _SR
    sig = 0.25 * rng.standard_normal(n) + 0.4 * np.sin(2 * np.pi * 220 * t)
    return (sig / np.max(np.abs(sig)) * 0.8).astype(np.float32)


def _hops(sig: np.ndarray):
    return [
        sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]
        for k in range(len(sig) // ANALYSIS_HOP)
    ]


# --- mono parity ---------------------------------------------------------------


def test_push_stereo_of_identical_channels_matches_push():
    sig = _music()
    a_mono, a_st = Analyzer(), Analyzer()
    for hop in _hops(sig):
        fm = a_mono.push(hop)
        fs = a_st.push_stereo(hop, hop)
        assert fm.bands == fs.bands
        assert fm.energy == fs.energy
        assert fm.flux == fs.flux
        assert fm.melbank == fs.melbank
        assert fm.bass_beat == fs.bass_beat
        assert fm.salience == fs.salience
        assert fm.onset_width == fs.onset_width
        # Duplicated channels = perfectly centred: pan ~ 0 everywhere.
        if fs.pan:
            assert max(abs(p) for p in fs.pan) < 1e-5


def test_hard_panned_tone_reports_signed_pan():
    n = int(_SR * 3)
    t = np.arange(n) / _SR
    tone = (0.6 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    quiet = (0.006 * tone).astype(np.float32)  # not digital silence, hard-panned

    right = Analyzer()
    frame = None
    for lhop, rhop in zip(_hops(quiet), _hops(tone)):
        frame = right.push_stereo(lhop, rhop)
    assert frame is not None and frame.pan
    assert max(frame.pan) > 0.7  # the tone's bins report hard right

    left = Analyzer()
    for lhop, rhop in zip(_hops(tone), _hops(quiet)):
        frame = left.push_stereo(lhop, rhop)
    assert min(frame.pan) < -0.7  # and hard left when swapped


def test_silence_keeps_pan_empty():
    a = Analyzer()
    z = np.zeros(ANALYSIS_HOP, dtype=np.float32)
    for _ in range(50):
        frame = a.push_stereo(z, z)
    assert frame.pan == []


# --- track-map format ------------------------------------------------------------


def _map_with_pan(n: int = 600) -> TrackMap:
    pan = np.zeros((n, MELBANK_BINS), dtype=np.int8)
    pan[:, 0] = -100  # lows left
    pan[:, -1] = 100  # highs right
    f = TrackFeatures(
        bands=np.full((n, 5), 0.5, dtype=np.float32),
        energy=np.full(n, 0.5, dtype=np.float32),
        flux=np.zeros(n, dtype=np.float32),
        bass_flux=np.zeros(n, dtype=np.float32),
        mid_flux=np.zeros(n, dtype=np.float32),
        centroid=np.full(n, 0.5, dtype=np.float32),
        melbank=np.full((n, MELBANK_BINS), 0.4, dtype=np.float32),
        width=np.full(n, 0.8, dtype=np.float32),
        pan=pan,
    )
    beats = np.arange(0.5, n * _DT, 0.5)
    return TrackMap(
        duration=n * _DT, bpm=120.0, confidence=0.8, beats=beats,
        accents=np.full(beats.size, 0.8), downbeat=0, features=f,
    )


def test_v4_roundtrip_preserves_pan(tmp_path):
    p = tmp_path / "map.npz"
    tm = _map_with_pan()
    tm.save(p)
    loaded = TrackMap.load(p)
    assert loaded is not None and loaded.features is not None
    assert np.array_equal(loaded.features.pan, tm.features.pan)
    frame = loaded.frame_at(1.0)
    assert frame is not None and len(frame.pan) == MELBANK_BINS
    assert frame.pan[0] < -0.7 and frame.pan[-1] > 0.7


def test_pre_v4_cache_loads_with_empty_pan(tmp_path):
    # Rewrite a saved map as a v3 file without the pan row: it must still load
    # (the pre-warmed library is never invalidated), just without pan.
    p4 = tmp_path / "v4.npz"
    _map_with_pan().save(p4)
    with np.load(p4, allow_pickle=False) as z:
        data = {k: z[k] for k in z.files if k != "f_pan"}
    data["meta"] = data["meta"].copy()
    data["meta"][4] = 3.0
    p3 = tmp_path / "v3.npz"
    np.savez_compressed(p3, **data)
    loaded = TrackMap.load(p3)
    assert loaded is not None and loaded.features is not None
    assert loaded.features.pan.shape[0] == 0
    frame = loaded.frame_at(1.0)
    assert frame is not None and frame.pan == []


# --- render: the correct side lights ---------------------------------------------


def _pan_frame(pan_value: float) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.5, "bass": 0.5, "low_mid": 0.4, "mid": 0.4, "high": 0.4},
        energy=0.6,
        melbank=[0.5] * MELBANK_BINS,
        pan=[pan_value] * MELBANK_BINS,
        salience=1.0,
    )


def test_right_panned_content_brightens_right_lamp():
    chans = [
        EntertainmentChannel(channel_id=0, x=-1.0, y=0.0, z=0.5),
        EntertainmentChannel(channel_id=1, x=1.0, y=0.0, z=0.5),
    ]
    eng = EffectEngine(chans)
    eng.set_mode(SyncMode.INTENSE)  # unified room, pan_gain 0.5
    out = {}
    for _ in range(100):
        out = eng.render(_pan_frame(1.0), _DT)
    right = max(out[1])
    left = max(out[0])
    assert right > left * 1.2  # everything panned right: right lamp clearly wins

    # Mono frames (no pan) keep the room symmetric — the compat guarantee.
    eng2 = EffectEngine(chans)
    eng2.set_mode(SyncMode.INTENSE)
    for _ in range(100):
        out = eng2.render(_pan_frame(0.0), _DT)
    assert abs(max(out[0]) - max(out[1])) < 0.05
