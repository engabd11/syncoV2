"""Event selection precision: salience proportionality + vocal width rejection.

The upgrade that separates "reacts to sound" from "reacts to the music": flash
amplitude follows the frame's ABSOLUTE track-relative loudness (a quiet pluck
pulses small, the drop slams full), and detected onsets whose flux is too
narrowband to be percussion (sung vowels, sustained tones) are muted per mode.
"""

from __future__ import annotations

import numpy as np

from hue_music_sync.audio.analyzer import (
    MIN_ONSET_FLUX,
    AnalysisFrame,
    Analyzer,
)
from hue_music_sync.audio.tempo import BeatGrid
from hue_music_sync.const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, SyncMode
from hue_music_sync.effects.engine import EffectEngine
from hue_music_sync.effects.modes import MODE_PARAMS, event_gates
from hue_music_sync.hue.bridge import EntertainmentChannel

_SR = ANALYSIS_SAMPLE_RATE
_DT = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE


def _run(sig: np.ndarray) -> list[AnalysisFrame]:
    a = Analyzer()
    return [
        a.push(sig[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
        for k in range(len(sig) // ANALYSIS_HOP)
    ]


def _kicks(seconds: float, amp: float, bpm: float = 120.0, t0: float = 0.25) -> np.ndarray:
    """Kick thumps with a broadband attack click (like a real kick, not a sine)."""
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    rng = np.random.default_rng(7)
    click_len = int(0.01 * _SR)
    body_len = int(0.1 * _SR)
    body = np.sin(2 * np.pi * 60.0 * np.arange(body_len) / _SR) * np.exp(
        -np.arange(body_len) / (0.03 * _SR)
    )
    for bt in np.arange(t0, seconds, 60.0 / bpm):
        i = int(bt * _SR)
        seg = body[: len(sig) - i] * amp
        sig[i : i + len(seg)] += seg.astype(np.float32)
        click = rng.standard_normal(click_len) * 0.4 * amp
        seg = click[: len(sig) - i]
        sig[i : i + len(seg)] += seg.astype(np.float32)
    return sig


def _vowel_swells(seconds: float, amp: float = 0.6, f0: float = 250.0) -> np.ndarray:
    """Vowel-like tone clusters (3-4 harmonics) swelling over ~100 ms."""
    sig = np.zeros(int(_SR * seconds), dtype=np.float32)
    dur = int(0.35 * _SR)
    t = np.arange(dur) / _SR
    tone = sum(
        g * np.sin(2 * np.pi * f0 * h * t)
        for h, g in ((1, 1.0), (2, 0.6), (3, 0.4), (4, 0.25))
    )
    attack = np.minimum(1.0, np.arange(dur) / (0.1 * _SR))  # 100 ms swell
    seg0 = (tone * attack * np.exp(-t / 0.25) * amp).astype(np.float32)
    for bt in np.arange(0.3, seconds, 0.55):
        i = int(bt * _SR)
        seg = seg0[: len(sig) - i]
        sig[i : i + len(seg)] += seg
    return sig


# --- analyzer: absolute-loudness salience ------------------------------------

def test_salience_tracks_absolute_loudness():
    # Identical kicks at full level then at ~1/6 the level: the quiet section's
    # frames must report a proportionally small salience, not re-normalise to 1
    # the way the AGC'd bands do.
    loud = _kicks(20.0, amp=0.9)
    quiet = _kicks(15.0, amp=0.15, t0=0.0)
    frames = _run(np.concatenate([loud, quiet]))
    n_loud = int(20.0 / _DT)
    loud_sal = max(f.salience for f in frames[: n_loud])
    # Skip the first seconds of the quiet span (the smoothed RMS tail).
    quiet_sal = max(f.salience for f in frames[n_loud + int(2.0 / _DT) :])
    assert loud_sal > 0.85
    assert 0.03 < quiet_sal < 0.45  # proportional, neither dead nor full


def test_salience_resets_on_track_change():
    a = Analyzer()
    loud = _kicks(10.0, amp=0.9)
    for k in range(len(loud) // ANALYSIS_HOP):
        a.push(loud[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP])
    a.reset()
    quiet = _kicks(6.0, amp=0.15)
    sal = max(
        a.push(quiet[k * ANALYSIS_HOP : (k + 1) * ANALYSIS_HOP]).salience
        for k in range(len(quiet) // ANALYSIS_HOP)
    )
    # After reset the quiet track is its own reference: salience recovers.
    assert sal > 0.85


# --- analyzer: long-horizon threshold floor -----------------------------------

def test_threshold_floor_blocks_quiet_passage_wobble():
    # The mechanism itself: with a collapsed (near-zero) history the adaptive
    # threshold falls to MIN_ONSET_FLUX and small wobble "beats"; the
    # long-horizon floor must pin it above that.
    a = Analyzer()
    from collections import deque

    hist: deque[float] = deque([0.05] * 43, maxlen=43)
    wobble = MIN_ONSET_FLUX * 1.4  # clears the absolute floor
    beat, strength, _ = a._threshold_onset(wobble, hist, since=99)
    assert beat  # without the floor this fires (the old behaviour)
    hist = deque([0.05] * 43, maxlen=43)
    beat, strength, _ = a._threshold_onset(
        wobble, hist, since=99, floor=wobble * 2.0
    )
    assert not beat  # the passage-scale floor keeps it silent


# --- analyzer: onset width (percussive vs vocal/tonal) ------------------------

def test_onset_width_separates_drums_from_vowels():
    def width_at_onsets(sig: np.ndarray) -> float:
        frames = _run(sig)
        flux = np.array([f.flux for f in frames])
        # The widest frame among the strongest onsets (an onset can straddle
        # two hops; take the top decile of flux frames).
        strong = flux >= np.percentile(flux[flux > 0], 90)
        return max(f.onset_width for f, s in zip(frames, strong) if s)

    kick_w = width_at_onsets(_kicks(8.0, amp=0.8))
    vowel_w = width_at_onsets(_vowel_swells(8.0))
    assert kick_w > vowel_w + 0.10  # clear separation
    assert kick_w > MODE_PARAMS[SyncMode.HIGH].width_min  # drums pass High's gate


# --- mode gates ---------------------------------------------------------------

def test_event_gates_ladder_strictness():
    # Subtle strictest -> Extreme loosest, and salience never amplifies.
    sal, width = 0.25, 0.20
    amps = {}
    gates = {}
    for mode in (SyncMode.SUBTLE, SyncMode.MEDIUM, SyncMode.HIGH,
                 SyncMode.INTENSE, SyncMode.EXTREME):
        amp, gate = event_gates(MODE_PARAMS[mode], sal, width)
        amps[mode] = amp
        gates[mode] = gate
        assert 0.0 <= amp <= 1.0 and 0.0 <= gate <= 1.0
    assert amps[SyncMode.SUBTLE] < amps[SyncMode.HIGH] < amps[SyncMode.EXTREME]
    assert gates[SyncMode.SUBTLE] < gates[SyncMode.HIGH] <= gates[SyncMode.EXTREME]
    # Full salience always passes at full amplitude.
    for mode in amps:
        amp, _ = event_gates(MODE_PARAMS[mode], 1.0, 1.0)
        assert amp == 1.0


# --- engine: reactions proportional to real loudness ---------------------------

def _channels(n: int = 5) -> list[EntertainmentChannel]:
    return [
        EntertainmentChannel(
            channel_id=i, x=-1.0 + 2.0 * i / max(1, n - 1), y=0.0, z=0.0
        )
        for i in range(n)
    ]


def _bed(salience: float = 1.0, width: float = 1.0) -> AnalysisFrame:
    return AnalysisFrame(
        bands={"sub_bass": 0.5, "bass": 0.5, "low_mid": 0.3, "mid": 0.3, "high": 0.2},
        energy=0.5,
        salience=salience,
        onset_width=width,
    )


def _kick_frame(salience: float = 1.0, width: float = 1.0) -> AnalysisFrame:
    f = _bed(salience, width)
    f.beat = f.bass_beat = True
    f.beat_strength = f.bass_strength = 2.5
    return f


def _beat_lift(mode: SyncMode, kick: AnalysisFrame, bed: AnalysisFrame) -> float:
    """The beat's brightness LIFT over the continuous bed (reactive, no grid).

    The continuous melbank/band layer is deliberately untouched by the salience
    gates (the room must stay alive), so the flash contribution is measured as
    peak-over-bed, not the absolute peak.
    """
    eng = EffectEngine(_channels(5))
    eng.set_mode(mode)
    bedp = 0.0
    for _ in range(50):  # settle envelopes/roles on the bed
        out = eng.render(bed, _DT)
        bedp = max(bedp, max(max(c) for c in out.values()))
    peak = max(max(c) for c in eng.render(kick, _DT).values())
    for _ in range(8):
        out = eng.render(bed, _DT)
        peak = max(peak, max(max(c) for c in out.values()))
    return peak - bedp


def test_low_salience_beats_render_proportionally_small():
    full = _beat_lift(SyncMode.HIGH, _kick_frame(salience=1.0), _bed(1.0))
    small = _beat_lift(SyncMode.HIGH, _kick_frame(salience=0.15), _bed(0.15))
    assert full > 0.3  # a full-salience kick clearly lifts the room
    assert small < 0.55 * full  # a quiet one pulses visibly smaller


def test_narrowband_onsets_muted_per_mode():
    # A narrow (vocal/tonal-like) onset must be muted so it never flashes: High
    # cut it already, and Extreme now does too (the user's 'no vocal flashes').
    # A broadband hit still flashes in both.
    high = _beat_lift(SyncMode.HIGH, _kick_frame(width=0.11), _bed(1.0, 0.11))
    extreme = _beat_lift(SyncMode.EXTREME, _kick_frame(width=0.11), _bed(1.0, 0.11))
    wide = _beat_lift(SyncMode.HIGH, _kick_frame(width=0.6), _bed(1.0, 0.6))
    extreme_wide = _beat_lift(SyncMode.EXTREME, _kick_frame(width=0.6), _bed(1.0, 0.6))
    assert high <= 0.05  # muted: no flash above the continuous bed
    assert extreme <= 0.05  # Extreme mutes narrowband (vocal/tonal) onsets too now
    assert wide > high + 0.2  # a broadband hit still flashes in High
    assert extreme_wide > 0.2  # …and slams in Extreme


def _kick_strength(strength: float, width: float = 0.6) -> AnalysisFrame:
    f = _bed(1.0, width)
    f.beat = f.bass_beat = True
    f.beat_strength = f.bass_strength = strength
    return f


def test_extreme_shows_mid_beats_not_just_the_loudest():
    # The user's "it only takes the top half of the graph" fix (beat_floor): in
    # Extreme a mid-strength kick must flash nearly as hard as the biggest hit —
    # the whole dynamic range shows, not just the loud peaks — while a narrowband
    # (vocal/tonal) onset still stays muted.
    big = _beat_lift(SyncMode.EXTREME, _kick_strength(3.0), _bed(1.0, 0.6))
    mid = _beat_lift(SyncMode.EXTREME, _kick_strength(1.3), _bed(1.0, 0.6))
    narrow = _beat_lift(SyncMode.EXTREME, _kick_strength(1.3, 0.12), _bed(1.0, 0.12))
    assert mid > 0.7             # a mid kick is a real, hard flash (not a dim wiggle)
    assert mid > 0.85 * big      # nearly as hard as the biggest hit
    assert narrow < 0.1          # narrowband stays muted (width-gated floor)

    # High stays proportional/gentle — the hard floor is Extreme-only.
    hi_mid = _beat_lift(SyncMode.HIGH, _kick_strength(1.3), _bed(1.0, 0.6))
    assert hi_mid < mid          # Extreme's mid kick is clearly harder than High's


def _grid(predicted: bool, accent: float = 0.9, phase: float = 0.3) -> BeatGrid:
    return BeatGrid(
        bpm=120.0, confidence=0.9, locked=True, period_s=0.5, phase=phase,
        time_to_next_beat=(1.0 - phase) * 0.5, next_beat_t=0.0,
        bar_phase=phase / 4.0, predicted_beat=predicted,
        accent=accent, accent_now=accent, beat_in_bar=0,
    )


def test_scheduled_beats_scale_with_salience_too():
    # Grid-locked scheduled pulses in a quiet bridge must render small as well —
    # otherwise a locked grid would keep slamming through the breakdown.
    def lift(salience: float) -> float:
        eng = EffectEngine(_channels(5))
        eng.set_mode(SyncMode.EXTREME)
        bed = _bed(salience)
        bedp = 0.0
        for _ in range(50):
            out = eng.render(bed, _DT, beatgrid=_grid(False))
            bedp = max(bedp, max(max(c) for c in out.values()))
        p = max(max(c) for c in eng.render(bed, _DT, beatgrid=_grid(True)).values())
        for _ in range(8):
            out = eng.render(bed, _DT, beatgrid=_grid(False))
            p = max(p, max(max(c) for c in out.values()))
        return p - bedp

    assert lift(0.2) < 0.65 * lift(1.0)


def test_ten_light_room_stays_alive_with_strict_picks():
    # 10 channels, High mode, vocal-ish content that never passes the gates:
    # the continuous melbank layer must keep the room visibly alive.
    eng = EffectEngine(_channels(10))
    eng.set_mode(SyncMode.HIGH)
    bed = AnalysisFrame(
        bands={"sub_bass": 0.2, "bass": 0.3, "low_mid": 0.5, "mid": 0.5, "high": 0.3},
        energy=0.5,
        melbank=[0.5] * 16,
        salience=0.5,
        onset_width=0.10,  # everything narrowband: no flashes ever qualify
    )
    means = []
    for _ in range(100):
        out = eng.render(bed, _DT)
        means.append(np.mean([max(c) for c in out.values()]))
    assert np.mean(means[50:]) > 0.05  # the room glows with the music
