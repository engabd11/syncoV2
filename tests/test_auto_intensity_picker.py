"""Musical Auto-intensity picker: what the SONG is -> which rung, gated to an
enabled set. Pure logic (no Home Assistant), so the character model, the band it
earns, the remap onto the enabled set and the anti-flicker behaviour are unit-
tested directly.

The headline contract these tests pin down: the rung follows the music's real
character, not each track's self-relative loudness. A chill track's own loudest
moment must NOT reach Intense/Extreme just because it is that track's p95.
"""

from __future__ import annotations

import pytest

from hue_music_sync.const import DEFAULT_AUTO_LEVELS, INTENSITY_LADDER, SyncMode
from hue_music_sync.effects.modes import (
    _CHAR_NEUTRAL,
    _PICK_DWELL_S,
    AutoIntensityPicker,
    _character_band,
    sanitize_auto_levels,
    song_character,
)

DEFAULT = tuple(DEFAULT_AUTO_LEVELS)                       # Subtle/Medium/High
WITH_INTENSE = DEFAULT + (SyncMode.INTENSE,)
ALL_RUNGS = WITH_INTENSE + (SyncMode.EXTREME,)
TOP_RUNGS = (SyncMode.HIGH, SyncMode.INTENSE, SyncMode.EXTREME)

# Song archetypes: the absolute character score each one earns. Named so the
# assertions below read as "a lofi track must never reach Intense", which is the
# behaviour being protected.
LOFI = 0.15        # lofi / very chill
INDIE = 0.40       # soft indie / acoustic
HOUSE = 0.60       # pop / house
EDM = 0.85         # EDM / heavy


def _run(
    picker: AutoIntensityPicker,
    allowed: tuple[SyncMode, ...],
    *,
    energy: float,
    salience: float,
    bpm: float,
    beat_period_s: float = 0.4,
    seconds: float = 6.0,
    dt: float = 0.02,
    profile: dict | None = None,
) -> SyncMode:
    """Drive the picker with a steady synthetic feature stream; return the
    settled rung. Long enough to clear the dwell and the smoothing. ``profile``
    (a per-song ``lo``/``hi``/``dynamics``/``mood``/``character`` dict) is passed
    straight through to :meth:`AutoIntensityPicker.update`; omit it for the
    fixed-window (live-tap) path."""
    t = 0.0
    last_beat = -1e9
    level = None
    for _ in range(int(seconds / dt)):
        beat = (t - last_beat) >= beat_period_s
        if beat:
            last_beat = t
        level = picker.update(
            dt, energy=energy, salience=salience, bpm=bpm, beat=beat,
            allowed=allowed, **(profile or {}),
        )
        t += dt
    return level


def _at(character: float, position: float, allowed=ALL_RUNGS) -> SyncMode:
    """The rung a song of ``character`` gets at ``position`` (0 = its quietest
    passage, 1 = its biggest moment). Resolved from a fresh picker so there is no
    hysteresis history — the pure map."""
    return AutoIntensityPicker()._resolve(
        position, allowed, 0.0, 1.0, 0.4, 0.0, character
    )


def _sweep(character: float, allowed=ALL_RUNGS) -> set[SyncMode]:
    """Every rung a song of ``character`` visits across its whole arc."""
    return {_at(character, i / 20.0, allowed) for i in range(21)}


def _top(character: float, allowed=ALL_RUNGS) -> SyncMode:
    return max(_sweep(character, allowed), key=INTENSITY_LADDER.index)


def _floor(character: float, allowed=ALL_RUNGS) -> SyncMode:
    return min(_sweep(character, allowed), key=INTENSITY_LADDER.index)


def _share(character: float, allowed=ALL_RUNGS) -> dict[SyncMode, float]:
    """Fraction of an even sweep across the song's arc spent on each rung."""
    picks = [_at(character, i / 100.0, allowed) for i in range(101)]
    return {m: picks.count(m) / len(picks) for m in allowed}


# --- the headline fix: character decides how high a song may go --------------

def test_a_chill_song_never_reaches_the_top_even_with_everything_enabled():
    # THE regression. A lofi track's own chorus normalises to 1.0 exactly like an
    # EDM drop does (energy is p95-normalised at analysis), and the picker used
    # to hand it the top enabled rung for that. It must now cap at Medium.
    assert _top(LOFI) is SyncMode.MEDIUM
    assert SyncMode.INTENSE not in _sweep(LOFI)
    assert SyncMode.EXTREME not in _sweep(LOFI)


def test_each_archetype_lands_in_its_own_band():
    assert _sweep(LOFI) == {SyncMode.SUBTLE, SyncMode.MEDIUM}
    assert _sweep(INDIE) == {SyncMode.MEDIUM, SyncMode.HIGH}
    assert _sweep(HOUSE) == {SyncMode.HIGH, SyncMode.INTENSE}
    assert _sweep(EDM) == {SyncMode.HIGH, SyncMode.INTENSE, SyncMode.EXTREME}


def test_extreme_is_earned_by_the_song_and_only_at_a_peak():
    # Only a genuinely heavy track reaches Extreme...
    assert _top(EDM) is SyncMode.EXTREME
    for character in (LOFI, INDIE, HOUSE):
        assert _top(character) is not SyncMode.EXTREME
    # ...and even then only at the very top of its arc, not through the chorus.
    assert _at(EDM, 1.0) is SyncMode.EXTREME
    assert _at(EDM, 0.9) is not SyncMode.EXTREME


def test_higher_character_never_lowers_the_ceiling():
    # The character -> band map is monotonic, so there is no character at which
    # a heavier song would be granted a *smaller* range than a lighter one.
    order = INTENSITY_LADDER.index
    tops = [order(_top(c / 20.0)) for c in range(21)]
    assert tops == sorted(tops)
    bands = [_character_band(c / 20.0) for c in range(21)]
    assert [f for f, _ in bands] == sorted(f for f, _ in bands)
    assert [t for _, t in bands] == sorted(t for _, t in bands)


# --- the vibe-gated floor ----------------------------------------------------

def test_an_energetic_song_never_drops_to_subtle():
    # Subtle is for music that is genuinely soft, not for the quiet bar of a
    # house track. With Subtle enabled it still must not be used on one.
    assert SyncMode.SUBTLE not in _sweep(HOUSE)
    assert SyncMode.SUBTLE not in _sweep(EDM)
    assert _floor(HOUSE) is SyncMode.HIGH


def test_a_chill_song_does_use_the_low_end():
    # The flip side: Subtle is not dead code — a genuinely soft track reaches it.
    assert _floor(LOFI) is SyncMode.SUBTLE


# --- the enabled set is a palette, remapped (not clipped) --------------------

def test_a_narrow_selection_is_rescaled_not_clipped():
    # With Subtle..High the ladder is rescaled onto three rungs: the heavy track
    # reaches the top of the selection, the chill one still does not.
    assert _top(EDM, DEFAULT) is SyncMode.HIGH
    assert _top(HOUSE, DEFAULT) is SyncMode.HIGH
    assert _top(LOFI, DEFAULT) is SyncMode.MEDIUM
    assert SyncMode.HIGH not in _sweep(LOFI, DEFAULT)


def test_the_lowest_enabled_rung_is_still_the_effective_floor():
    # Selecting High..Extreme makes High the floor — nothing drops below it.
    # Excluding the low rungs is a request to run hot, so the ladder rescales
    # upward: a soft track's chorus may now touch Intense, which it never would
    # with the full set enabled.
    for character in (LOFI, INDIE, HOUSE, EDM):
        assert _floor(character, TOP_RUNGS) is SyncMode.HIGH
    assert _top(HOUSE, TOP_RUNGS) is SyncMode.INTENSE
    assert _top(EDM, TOP_RUNGS) is SyncMode.EXTREME


def test_intense_and_extreme_need_enabling():
    for character in (HOUSE, EDM):
        assert SyncMode.INTENSE not in _sweep(character, DEFAULT)
        assert SyncMode.EXTREME not in _sweep(character, WITH_INTENSE)
    assert _top(EDM, WITH_INTENSE) is SyncMode.INTENSE


def test_a_sparse_selection_still_spreads():
    # Two far-apart rungs: both tracks rest on the low one, and the banger is the
    # one that actually lives up top. The chill track only reaches Extreme at the
    # very peak of its arc, because with nothing enabled between the two there is
    # no gentler rung for its chorus to use.
    allowed = (SyncMode.MEDIUM, SyncMode.EXTREME)
    assert _floor(LOFI, allowed) is SyncMode.MEDIUM
    assert _floor(EDM, allowed) is SyncMode.MEDIUM
    assert _top(EDM, allowed) is SyncMode.EXTREME
    assert _share(LOFI, allowed)[SyncMode.EXTREME] < 0.25
    assert _share(EDM, allowed)[SyncMode.EXTREME] > _share(LOFI, allowed)[SyncMode.EXTREME]


# --- the regression: Auto must never sit on one rung for a whole song --------

def test_every_selection_moves_on_every_song():
    # THE bug this fixes. `_character_band` works on the full five-rung axis
    # ("0.30 = the bottom of High") while the cells are renormalised over the
    # enabled set, so with High/Intense/Extreme picked, High's cell ran 0..0.49
    # and every band below that collapsed onto it: the room sat on the lowest
    # enabled rung for the entire song, whatever the music did.
    selections = [
        DEFAULT, WITH_INTENSE, ALL_RUNGS, TOP_RUNGS,
        (SyncMode.MEDIUM, SyncMode.HIGH, SyncMode.INTENSE),
        (SyncMode.HIGH, SyncMode.INTENSE),
        (SyncMode.INTENSE, SyncMode.EXTREME),
        (SyncMode.MEDIUM, SyncMode.EXTREME),
        (SyncMode.SUBTLE, SyncMode.INTENSE),
    ]
    for allowed in selections:
        for character in (0.0, LOFI, 0.25, INDIE, 0.5, HOUSE, 0.75, EDM, 1.0):
            visited = _sweep(character, allowed)
            assert len(visited) >= 2, (
                f"stuck on {visited} for character {character} with {allowed}"
            )


def test_a_selection_that_excludes_the_songs_rungs_still_rests_at_the_bottom():
    # Moving is not the same as running hot: with the low rungs excluded, a chill
    # track still spends most of its time on the lowest enabled rung and only
    # visits the next one at its peak.
    for allowed in (TOP_RUNGS, (SyncMode.HIGH, SyncMode.INTENSE)):
        share = _share(LOFI, allowed)
        assert share[SyncMode.HIGH] > 0.6
        assert share[SyncMode.INTENSE] > 0.0


def test_time_up_top_still_ranks_the_songs():
    # The remap must not flatten the character model: under one selection, the
    # heavier the track the more of its arc it spends above the floor rung.
    for allowed in (TOP_RUNGS, (SyncMode.HIGH, SyncMode.INTENSE)):
        above = [
            1.0 - _share(c, allowed)[SyncMode.HIGH]
            for c in (LOFI, INDIE, HOUSE, EDM)
        ]
        assert above == sorted(above), above


def test_a_full_selection_is_unchanged_by_the_remap():
    # With all five enabled the selection axis IS the full ladder, so the remap
    # is the identity and every earlier guarantee holds untouched.
    from hue_music_sync.effects.modes import _selection_knots, _to_selection

    src, dst = _selection_knots(tuple(INTENSITY_LADDER))
    for i in range(101):
        pos = i / 100.0
        assert _to_selection(pos, src, dst) == pytest.approx(pos, abs=1e-9)


def test_the_remap_is_monotonic_and_spans_the_selection():
    from hue_music_sync.effects.modes import _selection_knots, _to_selection

    for allowed in (DEFAULT, TOP_RUNGS, (SyncMode.MEDIUM, SyncMode.EXTREME),
                    (SyncMode.SUBTLE, SyncMode.INTENSE), (SyncMode.HIGH,)):
        src, dst = _selection_knots(tuple(allowed))
        vals = [_to_selection(i / 200.0, src, dst) for i in range(201)]
        assert vals == sorted(vals)
        assert vals[0] == pytest.approx(0.0)
        assert vals[-1] == pytest.approx(1.0)


def test_never_returns_a_rung_outside_the_enabled_set():
    for allowed in (DEFAULT, WITH_INTENSE, ALL_RUNGS, TOP_RUNGS, (SyncMode.HIGH,),
                    (SyncMode.SUBTLE, SyncMode.INTENSE)):
        for character in (0.0, LOFI, INDIE, HOUSE, EDM, 1.0):
            assert _sweep(character, allowed) <= set(allowed)


def test_single_enabled_rung_is_pinned():
    level = _run(
        AutoIntensityPicker(), (SyncMode.MEDIUM,),
        energy=1.0, salience=1.0, bpm=160, beat_period_s=0.25,
    )
    assert level is SyncMode.MEDIUM


def test_profile_never_leaves_the_enabled_set():
    # The clamp holds with a full per-song profile driving the pick, live.
    prof = dict(lo=0.2, hi=0.9, dynamics=0.7, mood=0.16, character=EDM)
    quiet = dict(energy=0.30, salience=0.30, bpm=92, beat_period_s=0.8)
    loud = dict(energy=1.0, salience=1.0, bpm=150, beat_period_s=0.25)
    for allowed in (DEFAULT, WITH_INTENSE, (SyncMode.HIGH,),
                    (SyncMode.SUBTLE, SyncMode.INTENSE)):
        for kw in (quiet, loud):
            level = _run(AutoIntensityPicker(), allowed, **kw, profile=prof,
                         seconds=14.0)
            assert level in allowed


# --- the moment inside the band ---------------------------------------------

def test_the_song_arc_moves_the_rung():
    # Character sets the band; the section curve still has to move within it, or
    # the room would sit on one rung all song.
    for character in (LOFI, INDIE, HOUSE, EDM):
        assert len(_sweep(character)) >= 2


def test_dynamics_knob_compresses_a_flat_song():
    # SAME character and arc; only the `dynamics` label differs. A flat,
    # constant-loudness track stays put rather than twitching between rungs.
    def sweep(dynamics):
        p = AutoIntensityPicker()
        return {p._resolve(i / 20.0, ALL_RUNGS, 0.0, 1.0, dynamics, 0.0, HOUSE)
                for i in range(21)}
    assert len(sweep(0.4)) > len(sweep(0.02))


def test_mood_slides_the_operating_point():
    # Mood is a deterministic shift of where in the band a moment sits: at the
    # same mid-arc position, a bass-heavy/fast (+mood) song rides up, a mellow
    # one down. Resolved fresh (no hysteresis history) so it's the pure map.
    order = INTENSITY_LADDER.index

    def pick(mood: float) -> SyncMode:
        return AutoIntensityPicker()._resolve(
            0.55, ALL_RUNGS, 0.30, 0.80, 0.5, mood, HOUSE
        )

    assert order(pick(0.16)) >= order(pick(0.0)) >= order(pick(-0.16))
    assert order(pick(0.16)) > order(pick(-0.16))


# --- the character score itself ---------------------------------------------

def test_character_is_monotonic_in_every_term():
    # The weights sum to 1.0, so an all-neutral song scores exactly neutral.
    base = dict(tempo=0.5, busy=0.5, attack=0.5, bass=0.5)
    assert song_character(**base) == pytest.approx(_CHAR_NEUTRAL)
    for term in base:
        low = song_character(**{**base, term: 0.0})
        high = song_character(**{**base, term: 1.0})
        assert high > _CHAR_NEUTRAL > low, term


def test_character_separates_the_archetypes():
    # A slow, sparse, soft-attack, bright track vs a fast, busy, percussive,
    # bass-heavy one.
    chill = song_character(tempo=0.1, busy=0.15, attack=0.1, bass=0.3)
    heavy = song_character(tempo=0.9, busy=0.85, attack=0.9, bass=0.8)
    assert chill < 0.25 < 0.75 < heavy
    assert _top(chill) in (SyncMode.SUBTLE, SyncMode.MEDIUM)
    assert _top(heavy) is SyncMode.EXTREME


def test_the_unreliable_terms_are_weighted_below_the_reliable_ones():
    # Tempo can be badly wrong (a half/double-time BPM lock is a real failure
    # mode) and spectral tilt is noisy (a bass-register drone reads "heavy" on
    # tilt alone). Neither may outweigh attack or busyness, which measure how
    # percussive and how active the music actually is.
    base = dict(tempo=0.5, busy=0.5, attack=0.5, bass=0.5)

    def swing(term):
        return (song_character(**{**base, term: 1.0})
                - song_character(**{**base, term: 0.0}))

    assert swing("tempo") < swing("busy")
    assert swing("bass") < swing("tempo")
    # And no single term can carry a song from one end of the ladder to the other.
    for term in base:
        assert swing(term) <= 0.35, term


# --- live estimation (no offline profile) -----------------------------------

def test_an_unknown_song_opens_mid_ladder_never_at_the_top():
    # A live tap has no profile: the first frames must not be able to earn
    # Extreme before the picker has heard enough of the track.
    p = AutoIntensityPicker()
    opening = p.update(
        0.02, energy=1.0, salience=1.0, bpm=175, beat=True, allowed=ALL_RUNGS,
        onset_width=1.0, centroid=0.0, flux=1.0,
    )
    assert opening is not SyncMode.EXTREME
    assert opening in (SyncMode.MEDIUM, SyncMode.HIGH)


def test_the_live_estimate_separates_a_soft_track_from_a_driving_one():
    # Fed long enough to clear the warm-up, the live path reaches the same
    # conclusion the offline profile would: soft music stays low, driving music
    # climbs. Compared as the estimate itself, so it isn't a dwell artefact.
    soft = AutoIntensityPicker()
    hard = AutoIntensityPicker()
    _run(soft, ALL_RUNGS, energy=0.45, salience=0.35, bpm=80,
         beat_period_s=1.0, seconds=40.0,
         profile=dict(onset_width=0.20, centroid=0.75, flux=0.08))
    _run(hard, ALL_RUNGS, energy=0.98, salience=0.95, bpm=150,
         beat_period_s=0.30, seconds=40.0,
         profile=dict(onset_width=0.55, centroid=0.25, flux=0.45))
    assert soft._character(0.0) < _CHAR_NEUTRAL < hard._character(1.0)
    assert INTENSITY_LADDER.index(soft.level) < INTENSITY_LADDER.index(hard.level)


def test_the_warm_up_climbs_calmly_rather_than_thrashing():
    # While the live estimate is still forming, the rung may move — that's the
    # picker learning the song, not thrash. It must still be a handful of steps
    # over the warm-up, and settled by the end of it.
    p = AutoIntensityPicker()
    t, last_beat, switches, prev, tail = 0.0, -1e9, 0, None, []
    for i in range(int(30.0 / 0.02)):
        beat = (t - last_beat) >= 0.3
        if beat:
            last_beat = t
        lvl = p.update(0.02, energy=0.95, salience=0.9, bpm=145, beat=beat,
                       allowed=ALL_RUNGS, onset_width=0.5, centroid=0.3, flux=0.4)
        if prev is not None and lvl is not prev:
            switches += 1
        prev = lvl
        if i > int(25.0 / 0.02):
            tail.append(lvl)
        t += 0.02
    assert switches <= 4
    assert len(set(tail)) == 1


def test_a_silent_passage_does_not_teach_the_estimator():
    # Character is only sampled while music is actually playing, so a long quiet
    # intro (or a gap between tracks) can't drag the estimate around.
    p = AutoIntensityPicker()
    _run(p, ALL_RUNGS, energy=0.0, salience=0.0, bpm=0.0,
         beat_period_s=99.0, seconds=30.0,
         profile=dict(onset_width=1.0, centroid=0.0, flux=1.0))
    assert p._char_age == 0.0


# --- anti-flicker: dwell + hysteresis ---------------------------------------

def test_does_not_thrash_between_rungs():
    """A steady stream settles and holds — few switches, none at the end.

    Character is supplied so this isolates the dwell/hysteresis; without it the
    live estimator is still warming up and a climb would be correct, not thrash.
    """
    p = AutoIntensityPicker()
    t = 0.0
    last_beat = -1e9
    switches = 0
    prev = None
    tail = []
    for i in range(int(8.0 / 0.02)):
        beat = (t - last_beat) >= 0.4
        if beat:
            last_beat = t
        lvl = p.update(
            0.02, energy=0.9, salience=0.85, bpm=130, beat=beat, allowed=ALL_RUNGS,
            character=HOUSE,
        )
        if prev is not None and lvl is not prev:
            switches += 1
        prev = lvl
        if i > int(6.0 / 0.02):
            tail.append(lvl)
        t += 0.02
    # It ramps up over a dwell or two, then holds rock-steady.
    assert switches <= 3
    assert len(set(tail)) == 1


def test_a_narrow_cell_is_still_reachable():
    # Extreme's cell is deliberately much narrower than the others. The
    # hysteresis dead-band scales with the cell, so it must not swallow it whole
    # — a peak on a heavy track still commits.
    p = AutoIntensityPicker()
    win = dict(lo=0.0, hi=1.0, dynamics=0.6, character=EDM, allowed=ALL_RUNGS)
    kw = dict(energy=0.0, salience=0.0, bpm=120, beat=False)
    for _ in range(400):
        p.update(0.02, signal=0.30, **kw, **win)
    assert p.level is SyncMode.HIGH
    reached = None
    for i in range(int(6.0 / 0.02)):
        if p.update(0.02, signal=1.0, **kw, **win) is SyncMode.EXTREME:
            reached = i * 0.02
            break
    assert reached is not None


def test_immediate_repick_applies_a_checklist_change_without_the_dwell():
    p = AutoIntensityPicker()
    kw = dict(energy=1.0, salience=1.0, bpm=160, beat=True, character=EDM,
              lo=0.0, hi=1.0, dynamics=0.6, signal=1.0)
    # Settle capped at High under the default set.
    for _ in range(300):
        p.update(0.02, allowed=DEFAULT, **kw)
    assert p.level is SyncMode.HIGH
    # Enabling Intense/Extreme + clearing the dwell lets the very next frame climb.
    p.allow_immediate_repick()
    level = p.update(0.02, allowed=ALL_RUNGS, **kw)
    assert level in (SyncMode.INTENSE, SyncMode.EXTREME)


# --- precomputed (lag-free) section signal: switch on time, not seconds late ---

def test_precomputed_signal_maps_directly_without_smoothing_lag():
    # The offline curve is mapped as given — one frame lands the rung, no
    # envelope ramp. A quiet value sits low, a loud value high, both immediately.
    win = dict(lo=0.0, hi=1.0, dynamics=0.6, character=EDM)
    low = AutoIntensityPicker().update(
        0.02, energy=0.0, salience=0.0, bpm=120, beat=False,
        allowed=ALL_RUNGS, signal=0.0, **win)
    high = AutoIntensityPicker().update(
        0.02, energy=0.0, salience=0.0, bpm=120, beat=False,
        allowed=ALL_RUNGS, signal=1.0, **win)
    assert low is SyncMode.HIGH        # bottom of an EDM track's band, frame 1
    assert high is SyncMode.EXTREME    # top of it, frame 1


def test_precomputed_signal_switches_within_the_dwell_not_seconds_after():
    # A step from quiet to loud is picked up as soon as the dwell allows (~3.5 s),
    # not delayed further by a slow envelope: the whole point of the offline curve.
    p = AutoIntensityPicker()
    win = dict(lo=0.0, hi=1.0, dynamics=0.6, character=HOUSE, allowed=ALL_RUNGS)
    kw = dict(energy=0.0, salience=0.0, bpm=120, beat=False)
    for _ in range(200):  # 4 s quiet -> settles at the bottom of its band
        p.update(0.02, signal=0.0, **kw, **win)
    assert p.level is SyncMode.HIGH
    # Now a hard section jump to a full chorus: within one dwell it reaches the top.
    reached = None
    for i in range(int(4.0 / 0.02)):
        if p.update(0.02, signal=1.0, **kw, **win) is SyncMode.INTENSE:
            reached = i * 0.02
            break
    assert reached is not None and reached <= _PICK_DWELL_S + 0.1


def test_reset_clears_the_carried_rung_and_the_character_estimate():
    # After a song settles high, reset() + allow_immediate_repick() lets the NEXT
    # song's opening rung apply on its first frame (no carry-forward), and the
    # live character estimate starts over so the previous track can't colour it.
    p = AutoIntensityPicker()
    win = dict(lo=0.0, hi=1.0, dynamics=0.6, character=EDM, allowed=ALL_RUNGS)
    kw = dict(energy=1.0, salience=1.0, bpm=150, beat=False)
    for _ in range(400):
        p.update(0.02, signal=1.0, **kw, **win)
    assert p.level is SyncMode.EXTREME
    p.reset()
    assert p.level is None          # nothing carried across
    assert p._char_age == 0.0       # nor the character estimate
    p.allow_immediate_repick()
    # A new, chill song opens quiet: correct on the very first frame.
    opening = p.update(0.02, signal=0.0, **kw, allowed=ALL_RUNGS,
                       lo=0.0, hi=1.0, dynamics=0.6, character=LOFI)
    assert opening is SyncMode.SUBTLE


# --- sanitising the user-supplied enabled set -------------------------------

def test_sanitize_orders_and_dedupes():
    assert sanitize_auto_levels(["high", "subtle", "subtle"]) == (
        SyncMode.SUBTLE, SyncMode.HIGH,
    )


def test_sanitize_accepts_enum_members():
    assert sanitize_auto_levels([SyncMode.EXTREME, SyncMode.MEDIUM]) == (
        SyncMode.MEDIUM, SyncMode.EXTREME,
    )


def test_sanitize_drops_auto_and_unknown_and_falls_back_when_empty():
    assert sanitize_auto_levels(["auto", "bogus"]) == tuple(DEFAULT_AUTO_LEVELS)
    assert sanitize_auto_levels([]) == tuple(DEFAULT_AUTO_LEVELS)
    assert sanitize_auto_levels(None) == tuple(DEFAULT_AUTO_LEVELS)
