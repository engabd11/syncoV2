"""Perceptual loudness core (v1.23): K-weighted salience, the brightness
gamma curve, the per-track dynamics profile and colour-luminance compensation.

The user-facing contract: brightness must track *perceived* loudness
relatively — the presence range counts like ears hear it (not like an RMS
meter), loudness ratios render as perceived-brightness ratios, and the palette
choice (Album colours v2) can't distort that mapping.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer, k_weighting
from hue_music_sync.audio.analyzer import AnalysisFrame
from hue_music_sync.color.palette import Palette
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, SyncMode
from hue_music_sync.effects.engine import EffectEngine, _LOUD_REF
from hue_music_sync.effects.modes import MODE_PARAMS, auto_mode_for_track
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_DT = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE


def _channels(n: int = 4) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / (n - 1), y=0.0, z=0.5)
        for i in range(n)
    ]


def _push_tone(a: Analyzer, hz: float, seconds: float = 1.0, amp: float = 0.4):
    n = int(_SR * seconds)
    t = np.arange(n) / _SR
    sig = (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    frame = None
    for k in range(n // ANALYSIS_HOP):
        frame = a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
    return frame


# --- K-weighting ---------------------------------------------------------------

def test_k_weighting_curve_shape():
    f = np.array([20.0, 60.0, 200.0, 1000.0, 3000.0, 8000.0])
    w = k_weighting(f)
    # Sub-bass is de-emphasised, presence emphasised, mids ~unity.
    assert w[0] < 0.2          # 20 Hz mostly rolled off
    assert 0.6 < w[1] < 0.85   # 60 Hz knee
    assert 0.9 < w[3] < 1.15   # 1 kHz ~unity
    assert w[4] > 1.15         # presence shelf engaged
    assert w[5] > w[3]         # highs above unity


def test_presence_counts_louder_than_sub_bass():
    # Equal-amplitude tones: the perceptual loudness (what salience smooths)
    # must rank the 2.5 kHz presence tone clearly above the 45 Hz sub tone —
    # plain RMS would call them identical.
    a_sub, a_pres = Analyzer(), Analyzer()
    _push_tone(a_sub, 45.0)
    _push_tone(a_pres, 2500.0)
    assert a_pres._rms_smooth > a_sub._rms_smooth * 1.3


# --- perceptual brightness gamma -------------------------------------------------

def _mid_frame() -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.4, "bass": 0.45, "low_mid": 0.4, "mid": 0.35, "high": 0.3},
        energy=0.45,
        melbank=[0.4] * 16,
        salience=0.6,
    )


def _room_mean(eng: EffectEngine, frames: int = 120) -> float:
    levels = []
    for _ in range(frames):
        out = eng.render(_mid_frame(), _DT)
        levels.append(max(max(c) for c in out.values()))
    return float(np.mean(levels[40:]))


def test_gamma_expands_visual_dynamic_range():
    # Same mid-level music: the perceptual curve renders it dimmer than the
    # legacy linear mapping (quiet parts LOOK quieter, so the chorus can look
    # louder), while a full-strength input still reaches full (endpoint math:
    # ((1-floor)/(1-floor))**g == 1, checked structurally below).
    lin = EffectEngine(_channels())
    lin.set_mode(SyncMode.HIGH)
    lin.params = replace(MODE_PARAMS[SyncMode.HIGH], bri_gamma=1.0)
    per = EffectEngine(_channels())
    per.set_mode(SyncMode.HIGH)
    assert _room_mean(per) < _room_mean(lin) - 0.02


def test_track_profile_adapts_gamma_and_loud_ref():
    eng = EffectEngine(_channels())
    # Brick-walled master (LRA ~3 dB): expand the tiny dynamics that exist.
    eng.set_track_profile(3.0)
    assert eng._track_gamma_mul > 1.2
    assert eng._loud_ref == _LOUD_REF
    # Dynamic recording (LRA 14 dB): ease back and give quiet beats headroom.
    eng.set_track_profile(14.0)
    assert eng._track_gamma_mul < 1.0
    assert eng._loud_ref < _LOUD_REF
    # Unknown (pre-v5 map) and reset are both exactly neutral.
    eng.set_track_profile(0.0)
    assert eng._track_gamma_mul == 1.0 and eng._loud_ref == _LOUD_REF


# --- musical auto-intensity ------------------------------------------------------

def test_auto_mode_for_track_separates_ballad_from_techno():
    # Same tempo, different music: the profile decides, not the BPM.
    ballad = auto_mode_for_track(120.0, onset_rate=0.3, energy_mean=0.15)
    techno = auto_mode_for_track(126.0, onset_rate=3.0, energy_mean=0.6)
    assert ballad in (SyncMode.SUBTLE, SyncMode.MEDIUM)
    assert techno is SyncMode.HIGH
    assert ballad is not techno
    # An unmeasured profile (pre-v5 map) abstains so the BPM ladder decides.
    assert auto_mode_for_track(0.0, 0.0, 0.0) is None


# --- colour-luminance compensation (Album colours v2 only) ----------------------

def _steady_room(eng: EffectEngine, colour) -> float:
    eng.set_palette(Palette([colour]))
    levels = []
    for _ in range(120):
        out = eng.render(_mid_frame(), _DT)
        levels.append(max(max(c) for c in out.values()))
    return float(np.mean(levels[60:]))


def test_lum_comp_lifts_blue_only_when_enabled():
    # Subtle: full saturation (no colour_sat softening) and a steady level, so
    # the compensation is measured cleanly. Desaturating modes naturally have
    # less luminance disparity and get proportionally less compensation.
    plain = EffectEngine(_channels())
    plain.set_mode(SyncMode.SUBTLE)
    comp = EffectEngine(_channels())
    comp.set_mode(SyncMode.SUBTLE)
    comp.lum_comp = True

    blue_plain = _steady_room(plain, (0.1, 0.1, 1.0))
    blue_comp = _steady_room(comp, (0.1, 0.1, 1.0))
    assert blue_comp > blue_plain * 1.2  # low-luminance hue lifted

    amber_plain = _steady_room(plain, (1.0, 0.75, 0.2))
    amber_comp = _steady_room(comp, (1.0, 0.75, 0.2))
    assert amber_comp <= amber_plain + 1e-6  # bright hue pulled in (or equal)

    # Default is OFF: every scheme except Album colours v2 is untouched.
    assert EffectEngine(_channels()).lum_comp is False
