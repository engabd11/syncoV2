"""Grid-first "conductor" behaviour: scheduled pulses, accents, vocal guards.

Once the tempo is known the *schedule* conducts: every grid beat is an event,
sized by its accent and the rank-based highlight selection (uniform passages
fire every beat — every kick IS the pulse there; dynamic mixes fire only the
standout ones). These tests pin that architecture and its guard rails.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import Analyzer, AnalysisFrame
from hue_music_sync.audio.tempo import BeatGrid, TempoTracker, _schedule_weight
from hue_music_sync.audio.trackmap import _rolling_accents, analyze_pcm
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS, ROLE_BASS, pulse_weight
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_DT = 0.02


def _channels(n: int = 3) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(channel_id=i, x=-1.0 + 2.0 * i / max(1, n - 1), y=0.0, z=0.0)
        for i in range(n)
    ]


def _quiet(bass_beat: bool = False, mid_beat: bool = False) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.2, "bass": 0.2, "low_mid": 0.2, "mid": 0.2, "high": 0.1},
        energy=0.4,
        bass_beat=bass_beat, bass_strength=2.0 if bass_beat else 0.0,
        mid_beat=mid_beat, mid_strength=2.0 if mid_beat else 0.0,
        beat=bass_beat or mid_beat, beat_strength=2.0 if (bass_beat or mid_beat) else 0.0,
    )


def _grid(predicted: bool, phase: float = 0.0, accent: float = 0.8,
          beat_in_bar: int = 0) -> BeatGrid:
    return BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5, phase=phase,
        time_to_next_beat=(1.0 - phase) * 0.5, next_beat_t=0.0,
        bar_phase=(beat_in_bar + phase) / 4.0, predicted_beat=predicted,
        accent=accent, accent_now=accent, beat_in_bar=beat_in_bar,
    )


# --- the conductor: scheduled beats fire without detection ------------------

def test_locked_grid_fires_pulse_without_detected_onset():
    # The core Samsung/Spotify property: the schedule conducts. A grid tick
    # with NO live-detected bass onset must still snap the bass lights.
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.INTENSE)
    for _ in range(10):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.4))
    roles = dict(eng.roles)
    bass_cid = next(c for c, r in roles.items() if r == ROLE_BASS)
    before = max(eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.9))[bass_cid])
    # The beat is a slew-limited swing; take its peak over the swell window.
    after = max(eng.render(_quiet(), _DT, beatgrid=_grid(True))[bass_cid])
    for _ in range(7):
        after = max(after, max(eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.1))[bass_cid]))
    assert after > before + 0.25  # the scheduled pulse landed


def test_every_locked_beat_pulses_no_misses():
    # 16 consecutive grid beats of EQUAL accent -> 16 visible bass-light rises,
    # zero skipped, with NO detected onsets at all (frame.bass_beat always
    # False). Uniform accents all rank as highlights: in a flat passage every
    # kick IS the pulse — selectivity must not eat a steady four-to-the-floor.
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.INTENSE)  # hard_snap: the flash carries the beat
    for _ in range(10):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.4))
    fired = 0
    for beat in range(16):
        pre_out = eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.96))
        tick_out = eng.render(_quiet(), _DT, beatgrid=_grid(True))
        # Roles may have rotated on this very tick; judge the current bass set.
        bass = [c for c, r in eng.roles.items() if r == ROLE_BASS]
        if any(max(tick_out[c]) > max(pre_out[c]) + 0.15 for c in bass):
            fired += 1
        for k in range(1, 24):  # let the flash decay before the next beat
            eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=k / 24.0))
    assert fired == 16


def test_locked_grid_still_reacts_to_off_grid_detected_onsets():
    # Regression: Music mode must react to the real detected beats even when a
    # locked (e.g. replayed track-map) grid is misaligned. Previously the locked
    # path required the onset to sit within a tight phase window of the grid, so
    # a slightly-off grid swallowed every real beat and the show went dead while
    # the audio was clearly pumping (Fireworks reacted, Music did not).
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.INTENSE)
    for _ in range(10):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.5))
    quiet_out = eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.5))
    # Locked grid mid-beat (no scheduled beat) + a genuine detected kick off-grid.
    onset = eng.render(_quiet(bass_beat=True), _DT, beatgrid=_grid(False, phase=0.5))
    assert (
        max(max(c) for c in onset.values())
        > max(max(c) for c in quiet_out.values()) + 0.2
    )


def test_unlocked_falls_back_to_reactive_kicks():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.MEDIUM)
    for _ in range(5):
        eng.render(_quiet(), _DT)  # no grid at all
    quiet_out = eng.render(_quiet(), _DT)
    kick_out = eng.render(_quiet(bass_beat=True), _DT)
    assert max(max(c) for c in kick_out.values()) > max(max(c) for c in quiet_out.values())


# --- pulse shaping: accents and the bar hierarchy ---------------------------

def test_pulse_weight_downbeat_beats_offbeats():
    p = MODE_PARAMS[SyncMode.MEDIUM]
    same_accent = 0.7
    w = [pulse_weight(p, same_accent, b) for b in range(4)]
    assert w[0] > w[1] and w[0] > w[3]  # downbeat hits hardest
    assert w[2] > w[1]  # beat 3 carries more than 2/4


def test_pulse_weight_non_highlights_still_tick_in_intense():
    p = MODE_PARAMS[SyncMode.INTENSE]
    # A beat that did NOT rank as a highlight keeps the quiet metronome tick.
    assert 0.0 < pulse_weight(p, 0.0, 1, highlight=False) < 0.4
    # A ranked highlight always lands a substantial pulse, even if its
    # absolute accent is small (quiet passages still get their hits).
    assert pulse_weight(p, 0.0, 1, highlight=True) > 0.4


def test_extreme_fires_every_beat_downbeat_hardest():
    # Club style: Extreme now punches on EVERY beat (ordinary beats included),
    # with the downbeat hardest and the top accents slamming anywhere in the bar.
    p = MODE_PARAMS[SyncMode.EXTREME]
    assert 0.0 < pulse_weight(p, 0.3, 1, highlight=False) < 0.4  # ordinary beat ticks
    assert pulse_weight(p, 0.3, 0, highlight=False) >= 0.55  # the "one" lands hard
    assert pulse_weight(p, 0.95, 2, highlight=True) > 0.5  # top accents slam anywhere


# --- live accent model -------------------------------------------------------

def _click_track(bpm: float, seconds: float) -> np.ndarray:
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    period = 60.0 / bpm
    for bt in np.arange(0.25, seconds, period):
        i = int(bt * _SR)
        env = np.exp(-np.arange(int(0.1 * _SR)) / (0.03 * _SR))
        seg = (np.sin(2 * np.pi * 60 * np.arange(len(env)) / _SR) * env).astype(np.float32)
        sig[i : i + len(seg)] += seg[: len(sig) - i]
    peak = np.max(np.abs(sig))
    return (sig / peak * 0.9) if peak else sig


def test_causal_tracker_predicts_normalised_accents():
    a = Analyzer()
    tt = TempoTracker(a.frame_period)
    sig = _click_track(120, 12.0)
    grid = None
    for k in range(len(sig) // ANALYSIS_HOP):
        f = a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        grid = tt.update(f.t_audio, f.flux, f.beat, f.beat_strength,
                         max(f.bands.get("sub_bass", 0.0), f.bands.get("bass", 0.0)))
    assert grid is not None and grid.locked
    # Uniform clicks: prediction settles high (every beat is "strong here").
    assert 0.5 <= grid.accent <= 1.0
    assert 0 <= grid.beat_in_bar <= 3


def test_schedule_weight_ramps_with_confidence():
    # A marginal causal lock pulses modestly; a solid one pulses at full.
    assert abs(_schedule_weight(0.40) - 0.6) < 1e-9
    assert _schedule_weight(0.50) == 0.8
    assert _schedule_weight(0.60) == 1.0
    assert _schedule_weight(0.95) == 1.0
    # The track map's grid never ramps (BeatGrid default).
    assert BeatGrid().schedule_strength == 1.0


def test_marginal_lock_pulses_smaller_than_solid_lock():
    # The confidence ramp (schedule_strength) scales the pulse. Use MEDIUM here:
    # the club modes' flashes are big enough to clamp near full, which would mask
    # the scaling - this is a tempo-confidence property, not a mode one.
    def pulse_at(weight: float) -> float:
        eng = EffectEngine(_channels(3))
        eng.set_mode(SyncMode.HIGH)
        for _ in range(10):
            eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.4))
        g = _grid(True)
        g.schedule_strength = weight
        # Peak over the beat's slew-limited swell, not the single tick frame.
        bass = [c for c, r in eng.roles.items() if r == ROLE_BASS]
        peak = max(max(eng.render(_quiet(), _DT, beatgrid=g)[c]) for c in bass)
        for _ in range(7):
            out = eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.1))
            peak = max(peak, max(max(out[c]) for c in bass))
        return peak

    assert pulse_at(1.0) > pulse_at(0.6) + 0.1


def test_agc_resets_between_tracks():
    # Loud track, then reset, then a much quieter track: the quiet track's own
    # peaks must still read near full scale immediately (the slow ~70 s AGC
    # decay otherwise leaves the next song dim for a minute).
    a = Analyzer()
    loud = _click_track(120, 4.0)
    for k in range(len(loud) // ANALYSIS_HOP):
        a.push(loud[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
    a.reset()
    quiet = (_click_track(120, 4.0) * 0.08).astype(np.float32)
    peak_bass = 0.0
    for k in range(len(quiet) // ANALYSIS_HOP):
        f = a.push(quiet[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        peak_bass = max(peak_bass, f.bands.get("bass", 0.0), f.bands.get("sub_bass", 0.0))
    assert peak_bass > 0.9


def test_rolling_accents_survive_one_huge_transient():
    # One mega onset (a drop impact) must not crush the surrounding kicks'
    # accents — the old global-max normalisation did exactly that.
    raw = np.full(64, 10.0)
    raw[32] = 100.0
    acc = _rolling_accents(raw)
    assert acc[10] > 0.9  # far from the spike: ordinary kick reads strong
    assert acc[56] > 0.9
    assert acc[32] == 1.0


# --- dense music keeps detecting ---------------------------------------------

def test_kicks_survive_a_dense_loud_bed():
    # Wall-of-sound: loud wobbling mid/high noise bed + kicks. The robust
    # median+MAD threshold must keep firing bass beats through the chorus
    # (mean+std used to ride up over the kicks and go silent).
    seconds = 8.0
    rng = np.random.default_rng(11)
    n = int(_SR * seconds)
    noise = rng.standard_normal(n).astype(np.float32)
    # Kill the noise's bass so the share gate sees kick-vs-bed clearly.
    spec = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / _SR)
    spec[freqs < 300.0] = 0.0
    bed = np.fft.irfft(spec, n).astype(np.float32)
    bed /= np.max(np.abs(bed))
    wobble = (1.0 + 0.3 * np.sin(2 * np.pi * 4.0 * np.arange(n) / _SR)).astype(np.float32)
    sig = 0.45 * bed * wobble
    period = 0.5
    for bt in np.arange(0.25, seconds, period):
        i = int(bt * _SR)
        env = np.exp(-np.arange(int(0.1 * _SR)) / (0.03 * _SR))
        seg = (0.9 * np.sin(2 * np.pi * 55 * np.arange(len(env)) / _SR) * env).astype(np.float32)
        sig[i : i + len(seg)] += seg[: len(sig) - i]
    sig = sig / np.max(np.abs(sig)) * 0.9

    a = Analyzer()
    beats = sum(
        a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]).bass_beat
        for k in range(len(sig) // ANALYSIS_HOP)
    )
    assert beats >= 8  # 16 kicks in 8 s; most must survive the bed


# --- vocal guards -------------------------------------------------------------

def test_slow_vowel_swell_does_not_pop_mid_lights():
    # A sung vowel: mid-band tone swelling in over ~150 ms. A pluck: the same
    # tone at full level from the first sample. Only the pluck may fire.
    seconds = 6.0

    def tone_events(attack_s: float) -> np.ndarray:
        sig = np.zeros(int(_SR * seconds), dtype=np.float32)
        for bt in np.arange(0.4, seconds, 0.75):
            i = int(bt * _SR)
            # Long enough to decay to ~silence before the segment ends —
            # an abrupt truncation IS a percussive click and would (rightly)
            # fire the detector.
            dur = int(0.50 * _SR)
            t = np.arange(dur) / _SR
            ramp = np.minimum(1.0, t / max(attack_s, 1e-4))
            decay = np.exp(-np.maximum(0.0, t - attack_s) / 0.08)
            seg = (0.8 * np.sin(2 * np.pi * 600 * t) * ramp * decay).astype(np.float32)
            sig[i : i + dur] += seg[: len(sig) - i]
        return sig

    a = Analyzer()
    swell = tone_events(attack_s=0.15)
    swell_mids = sum(
        a.push(swell[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]).mid_beat
        for k in range(len(swell) // ANALYSIS_HOP)
    )
    a = Analyzer()
    pluck = tone_events(attack_s=0.001)
    pluck_mids = sum(
        a.push(pluck[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]).mid_beat
        for k in range(len(pluck) // ANALYSIS_HOP)
    )
    assert pluck_mids >= 4  # percussive attacks pop
    assert swell_mids <= max(1, pluck_mids // 4)  # swells mostly don't


def test_locked_mid_pops_quantise_to_eighth_grid():
    eng = EffectEngine(_channels(3))
    eng.set_mode(SyncMode.HIGH)  # the mode that keeps the per-instrument split
    for _ in range(5):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.4))
    roles = dict(eng.roles)
    mid_cid = next(c for c, r in roles.items() if r == 1)

    # Mid onset right on the off-beat eighth (phase 0.5): pops.
    on_slot = eng.render(_quiet(mid_beat=True), _DT, beatgrid=_grid(False, phase=0.5))
    for _ in range(30):
        eng.render(_quiet(), _DT, beatgrid=_grid(False, phase=0.3))
    # Mid onset floating between slots (phase 0.27): a syllable — no pop.
    off_slot = eng.render(_quiet(mid_beat=True), _DT, beatgrid=_grid(False, phase=0.27))
    assert max(on_slot[mid_cid]) > max(off_slot[mid_cid]) + 0.15


def test_offline_mid_onsets_quantise_to_eighths():
    # Plucks deliberately off the eighth grid (0.3 of a beat after each kick)
    # must be dropped by the offline quantiser; on-grid plucks are kept (the
    # existing trackmap test covers the on-grid case).
    seconds = 30.0
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    kick_env = np.exp(-np.arange(int(0.1 * _SR)) / (0.03 * _SR))
    kick = (np.sin(2 * np.pi * 60 * np.arange(len(kick_env)) / _SR) * kick_env).astype(np.float32)
    for bt in np.arange(0.30, seconds, 0.5):
        i = int(bt * _SR)
        seg = kick[: len(sig) - i]
        sig[i : i + len(seg)] += seg
    pluck_env = np.exp(-np.arange(int(0.06 * _SR)) / (0.015 * _SR))
    pluck = (np.sin(2 * np.pi * 700 * np.arange(len(pluck_env)) / _SR) * pluck_env).astype(np.float32)
    # 0.2 beats after a kick: clearly off the eighth grid, and quiet + sparse
    # enough not to seduce the DP beat tracker away from the kicks.
    for bt in np.arange(0.30 + 0.10, seconds, 2.0):
        i = int(bt * _SR)
        seg = pluck[: len(sig) - i] * 0.5
        sig[i : i + len(seg)] += seg
    sig = sig / np.max(np.abs(sig)) * 0.9
    tm = analyze_pcm(sig)
    assert tm is not None and tm.beats.size >= 8
    # The off-grid syllable-like hits are filtered out almost entirely.
    assert tm.mid_beats.size <= 4
