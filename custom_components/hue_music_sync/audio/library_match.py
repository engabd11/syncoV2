"""Match a playing song to a track in the Music Assistant library, by metadata.

Synco resolves per-track audio through Music Assistant's *player* objects (queue
item -> streamdetails -> provider stream URL). That path is empty for a player
Music Assistant does not own - a Subsonic radio hitting the same Navidrome
server, say - so those songs land on the beat-less metadata animation even when
the exact same track has already been analysed by the library pre-warm.

All we get from such a player is what Home Assistant shows: title, artist,
album, duration. That is enough: if we can find *the same song* in the MA
library, the library Track hands us both the pre-warm's ``track_signature``
(so an already-analysed map is a straight cache hit) and a decodable library URL
(so an un-analysed one can still be analysed).

The danger is a *wrong* match - the lights would then run someone else's song -
so the scoring below is deliberately biased toward returning None: an exact
(normalised) title is only the entry ticket, and a match must be corroborated by
the artist or the duration, and must be unambiguous against the other
candidates. Falling back to the generic animation is a much cheaper mistake than
syncing to the wrong track.

Pure (re/unicodedata only), so the normalisation and scoring are unit-tested.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# A match must clear this, and beat the runner-up by MATCH_MARGIN.
MIN_SCORE = 0.55
MATCH_MARGIN = 0.20

# Duration tolerances (seconds). Tags and players round differently, so a couple
# of seconds is noise; a big gap means a different edit (live, extended, radio
# cut) whose analysis would be misaligned anyway - refuse it.
_DUR_EXACT_S = 2.0
_DUR_CLOSE_S = 5.0
_DUR_DISQUALIFY_S = 12.0

# Bracketed/trailing bits that name the same *recording* and so may be dropped
# when comparing titles. Deliberately excludes "live", "remix", "mix", "edit"
# and "version": those are different audio, and matching them onto the original
# would replay the wrong analysis. (The duration check would usually catch it -
# this is the belt to that braces.)
_SAFE_VERSION_RE = re.compile(
    r"\b(?:re-?mastere?d?|digitally\s+remastered|feat\.?|ft\.?|featuring"
    r"|explicit|bonus\s+track|deluxe)\b",
    re.IGNORECASE,
)
_BRACKETED_RE = re.compile(r"\s*[(\[][^()\[\]]*[)\]]")
_TRAILING_DASH_RE = re.compile(r"\s+[-–—]\s+[^-–—]+$")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")

# Artist separators: "A, B" / "A & B" / "A feat. B" / "AC/DC" all reduce to a
# token set, so any of those spellings on either side still intersect.
_ARTIST_SPLIT_RE = re.compile(
    r"[,;&/]|\s+(?:feat|ft|featuring|with|vs|x)\.?\s+", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class TrackQuery:
    """What a Home Assistant media_player reports about the song it is playing."""

    title: str | None
    artist: str | None = None
    album: str | None = None
    duration: float | None = None


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One Music Assistant library track, pre-digested for matching.

    ``signature`` is the pre-warm's ``track_signature`` for this track and is the
    point of the whole exercise. ``url`` may be None (no resolvable provider
    URL): keep such an entry anyway - its signature alone can still hit a map
    that was cached on disk when the track was played from Music Assistant.
    """

    signature: str
    title: str | None
    artist: str | None = None
    album: str | None = None
    duration: float | None = None
    url: str | None = None


def _strip_version(text: str) -> str:
    """Drop bracketed/trailing bits that don't change the recording."""
    out = _BRACKETED_RE.sub(
        lambda m: "" if _SAFE_VERSION_RE.search(m.group(0)) else m.group(0), text
    )
    tail = _TRAILING_DASH_RE.search(out)
    if tail is not None and _SAFE_VERSION_RE.search(tail.group(0)):
        out = out[: tail.start()]
    return out


def normalise(text: str | None) -> str:
    """Casefolded, unaccented, punctuation-free form used for all comparisons."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    folded = _strip_version(stripped).casefold()
    return " ".join(_NON_ALNUM_RE.sub(" ", folded).split())


def artist_tokens(artist: str | None) -> frozenset[str]:
    """The set of individual artist names in a credit string."""
    if not artist:
        return frozenset()
    parts = (normalise(p) for p in _ARTIST_SPLIT_RE.split(str(artist)))
    return frozenset(p for p in parts if p)


def title_key(title: str | None) -> str:
    """The index bucket a title falls in."""
    return normalise(title)


def query_key(query: TrackQuery) -> str:
    """A stable cache key for one player-reported song (raw fields, not normalised)."""
    return "|".join(
        "" if v is None else str(v)
        for v in (query.title, query.artist, query.album, query.duration)
    )


def _duration(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def build_index(entries) -> dict[str, list[LibraryEntry]]:
    """Bucket library entries by normalised title, ready for :func:`best_match`."""
    index: dict[str, list[LibraryEntry]] = {}
    for entry in entries:
        key = title_key(entry.title)
        if key:
            index.setdefault(key, []).append(entry)
    return index


def score(query: TrackQuery, entry: LibraryEntry) -> float | None:
    """How well a library entry matches the playing song; None = disqualified.

    An exact normalised title is required but never sufficient on its own: the
    match must be *corroborated* by an artist overlap or a near-equal duration,
    or we would happily light up to a completely different song that happens to
    share a common title.
    """
    if not title_key(query.title) or title_key(query.title) != title_key(entry.title):
        return None

    total = 0.30
    corroborated = False

    q_artists = artist_tokens(query.artist)
    e_artists = artist_tokens(entry.artist)
    if q_artists and e_artists:
        if q_artists == e_artists:
            total += 0.45
            corroborated = True
        elif q_artists & e_artists:
            total += 0.30
            corroborated = True
        else:
            return None  # same title, different artists: a cover, not our song

    q_dur = _duration(query.duration)
    e_dur = _duration(entry.duration)
    if q_dur is not None and e_dur is not None:
        delta = abs(q_dur - e_dur)
        if delta > _DUR_DISQUALIFY_S:
            return None  # a different edit; its analysis wouldn't line up anyway
        if delta <= _DUR_EXACT_S:
            total += 0.30
            corroborated = True
        elif delta <= _DUR_CLOSE_S:
            total += 0.15
            corroborated = True

    q_album = normalise(query.album)
    if q_album and q_album == normalise(entry.album):
        total += 0.15  # a bonus only - radios often report no album, or a different one

    return total if corroborated else None


def _same_recording(a: LibraryEntry, b: LibraryEntry) -> bool:
    """Whether two candidates are plausibly the same audio (so picking either is fine).

    The same track commonly sits in the library twice (an album and a greatest-
    hits, say). Those are not a genuine ambiguity - they have the same artist and
    the same length - so we take the better-scoring one instead of giving up. A
    cover (other artist) or another edit (other length) *is* ambiguous.
    """
    if artist_tokens(a.artist) != artist_tokens(b.artist):
        return False
    a_dur, b_dur = _duration(a.duration), _duration(b.duration)
    if a_dur is not None and b_dur is not None:
        return abs(a_dur - b_dur) <= _DUR_EXACT_S
    return True


def best_match(
    query: TrackQuery,
    index: dict[str, list[LibraryEntry]],
    *,
    min_score: float = MIN_SCORE,
    margin: float = MATCH_MARGIN,
) -> LibraryEntry | None:
    """The library entry this song is, or None when unmatched or ambiguous."""
    scored: list[tuple[float, LibraryEntry]] = []
    for entry in index.get(title_key(query.title), ()):
        value = score(query, entry)
        if value is not None:
            scored.append((value, entry))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)

    top_score, top = scored[0]
    if top_score < min_score:
        return None
    for value, entry in scored[1:]:
        if top_score - value >= margin:
            break  # the rest score lower still: the winner is clear
        if entry.signature != top.signature and not _same_recording(top, entry):
            return None  # a real ambiguity (cover / other edit): refuse to guess
    return top
