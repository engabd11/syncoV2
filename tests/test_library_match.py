"""Matching a playing song to a Music Assistant library track by its metadata.

The bias under test is "refuse rather than guess": a wrong match would run the
lights on a different song, which is far worse than falling back to the generic
metadata animation.
"""

from __future__ import annotations

import pytest

from hue_music_sync.audio.library_match import (
    LibraryEntry,
    TrackQuery,
    artist_tokens,
    best_match,
    build_index,
    normalise,
    query_key,
    title_key,
)


def _entry(sig, title, artist=None, album=None, duration=None, url="http://nav/1"):
    return LibraryEntry(
        signature=sig, title=title, artist=artist, album=album,
        duration=duration, url=url,
    )


_SONG = _entry(
    "library://track/7|Fleetwood Mac|Dreams",
    "Dreams", "Fleetwood Mac", "Rumours", 257.0,
)
_INDEX = build_index([_SONG])


# --- normalisation ---------------------------------------------------------

def test_normalise_folds_case_accents_and_punctuation():
    assert normalise("Björk – Jóga!") == normalise("bjork  joga")


def test_normalise_drops_version_suffixes_that_keep_the_same_recording():
    for title in (
        "Dreams (Remastered 2011)",
        "Dreams - 2004 Remaster",
        "Dreams [Explicit]",
        "Dreams (feat. Stevie Nicks)",
    ):
        assert title_key(title) == title_key("Dreams")


def test_normalise_keeps_suffixes_that_mean_different_audio():
    # A live take or a remix is *not* the same recording: matching it onto the
    # studio track would replay an analysis that doesn't line up with the audio.
    for title in ("Dreams (Live)", "Dreams (Club Remix)", "Dreams - Radio Edit"):
        assert title_key(title) != title_key("Dreams")


def test_artist_tokens_split_on_every_credit_spelling():
    expected = frozenset({"stevie nicks", "lindsey buckingham"})
    for credit in (
        "Stevie Nicks, Lindsey Buckingham",
        "Stevie Nicks & Lindsey Buckingham",
        "Stevie Nicks feat. Lindsey Buckingham",
        "Stevie Nicks / Lindsey Buckingham",
    ):
        assert artist_tokens(credit) == expected


def test_query_key_is_stable():
    q = TrackQuery("Dreams", "Fleetwood Mac", "Rumours", 257.0)
    assert query_key(q) == query_key(TrackQuery("Dreams", "Fleetwood Mac", "Rumours", 257.0))
    assert query_key(q) != query_key(TrackQuery("Dreams", "Fleetwood Mac", "Rumours", 190.0))


# --- matching --------------------------------------------------------------

def test_exact_metadata_matches_and_returns_the_prewarm_signature():
    match = best_match(TrackQuery("Dreams", "Fleetwood Mac", "Rumours", 257.0), _INDEX)
    assert match is not None
    assert match.signature == _SONG.signature


def test_tag_noise_still_matches():
    # The radio reports a decorated title, a different credit spelling and a
    # duration rounded a second out - all the same song.
    match = best_match(
        TrackQuery("Dreams (Remastered 2011)", "Fleetwood Mac", None, 256.0), _INDEX
    )
    assert match is not None and match.signature == _SONG.signature


def test_same_title_different_artist_is_refused():
    assert best_match(TrackQuery("Dreams", "The Cranberries", None, 257.0), _INDEX) is None


def test_live_version_duration_gap_is_refused():
    assert best_match(TrackQuery("Dreams", "Fleetwood Mac", None, 297.0), _INDEX) is None


def test_title_alone_is_never_enough():
    # No artist and no duration to corroborate it: refuse, however unique the title.
    assert best_match(TrackQuery("Dreams", None, None, None), _INDEX) is None


def test_ambiguous_cover_pair_is_refused():
    # Two plausible candidates that are genuinely different recordings (the query
    # carries no artist to tell them apart) - guessing is not allowed.
    index = build_index([
        _entry("sig-a", "Dreams", "Fleetwood Mac", "Rumours", 257.0),
        _entry("sig-b", "Dreams", "The Corrs", "Talk on Corners", 256.0),
    ])
    assert best_match(TrackQuery("Dreams", None, None, 257.0), index) is None


def test_duplicate_library_entries_of_the_same_song_still_match():
    # The same recording listed twice (album + greatest hits) is not an ambiguity.
    index = build_index([
        _entry("sig-album", "Dreams", "Fleetwood Mac", "Rumours", 257.0),
        _entry("sig-hits", "Dreams", "Fleetwood Mac", "The Very Best Of", 257.0),
    ])
    match = best_match(TrackQuery("Dreams", "Fleetwood Mac", None, 257.0), index)
    assert match is not None
    assert match.signature in ("sig-album", "sig-hits")


def test_album_breaks_the_tie_between_duplicates():
    index = build_index([
        _entry("sig-album", "Dreams", "Fleetwood Mac", "Rumours", 257.0),
        _entry("sig-hits", "Dreams", "Fleetwood Mac", "The Very Best Of", 257.0),
    ])
    match = best_match(TrackQuery("Dreams", "Fleetwood Mac", "Rumours", 257.0), index)
    assert match is not None and match.signature == "sig-album"


def test_entry_without_a_url_is_still_matchable():
    # No provider URL to analyse from, but the signature alone can hit a map the
    # pre-warm already cached on disk - so the entry must stay in the index.
    index = build_index([_entry("sig-x", "Dreams", "Fleetwood Mac", None, 257.0, url=None)])
    match = best_match(TrackQuery("Dreams", "Fleetwood Mac", None, 257.0), index)
    assert match is not None and match.url is None


@pytest.mark.parametrize("query", [
    TrackQuery(None, "Fleetwood Mac", None, 257.0),
    TrackQuery("", "Fleetwood Mac", None, 257.0),
    TrackQuery("Not In The Library", "Fleetwood Mac", None, 257.0),
])
def test_unmatchable_queries_return_none(query):
    assert best_match(query, _INDEX) is None
