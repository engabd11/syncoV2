"""Direct OpenSubsonic / Navidrome library backend.

Browses and searches an OpenSubsonic server (e.g. Navidrome) straight over its
REST API — no Music Assistant in the path — so the player keeps working when
Home Assistant / Music Assistant is unavailable, and (later) can decode the
original lossless file locally for bit-perfect playback.

The REST calls reuse the token-auth + URL builders in
:mod:`hue_music_sync.audio.subsonic`. Response parsing is split into pure
functions (a parsed JSON payload → :class:`MediaItem`s) so it is unit-testable
without a live server; only :meth:`SubsonicBackend._get` touches the network.
"""

from __future__ import annotations

import logging
from typing import Callable

import aiohttp

from ..audio.subsonic import (
    subsonic_cover_art_url,
    subsonic_rest_url,
    subsonic_stream_url,
)
from .base import (
    BackendError,
    BrowseNode,
    LibraryBackend,
    MediaItem,
    MediaKind,
    SearchResults,
)

_LOGGER = logging.getLogger(__name__)

CoverUrl = Callable[[object], str | None]

# Root-level album shelves: (browse-node id, card title, getAlbumList2 type).
_ALBUM_LISTS: tuple[tuple[str, str, str], ...] = (
    ("albums:newest", "Recently Added", "newest"),
    ("albums:recent", "Recently Played", "recent"),
    ("albums:frequent", "Most Played", "frequent"),
    ("albums:alphabetical", "Albums A–Z", "alphabeticalByName"),
    ("albums:random", "Random Albums", "random"),
)
_ALBUM_LIST_TYPES = {node_id: kind for node_id, _title, kind in _ALBUM_LISTS}
_ALBUM_LIST_TITLES = {node_id: title for node_id, title, _kind in _ALBUM_LISTS}

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


# --- pure response parsing ------------------------------------------------

def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _count_line(count: object, noun: str) -> str | None:
    n = _as_int(count)
    return f"{n} {noun}" if n else None


def _artist_item(d: dict, cover_url: CoverUrl) -> MediaItem:
    return MediaItem(
        id=f"artist:{d.get('id')}",
        kind=MediaKind.ARTIST,
        title=str(d.get("name") or "Unknown artist"),
        subtitle=_count_line(d.get("albumCount"), "albums"),
        artwork=cover_url(d.get("coverArt") or d.get("id")),
        browsable=True,
    )


def _album_item(d: dict, cover_url: CoverUrl) -> MediaItem:
    return MediaItem(
        id=f"album:{d.get('id')}",
        kind=MediaKind.ALBUM,
        title=str(d.get("name") or d.get("album") or "Unknown album"),
        subtitle=(str(d.get("artist")) if d.get("artist") else None),
        artwork=cover_url(d.get("coverArt") or d.get("id")),
        browsable=True,
    )


def _song_item(d: dict, cover_url: CoverUrl) -> MediaItem:
    # The raw song id is what /rest/stream and the local decoder consume, so a
    # track item carries it unprefixed (unlike the browsable artist:/album: ids).
    return MediaItem(
        id=str(d.get("id")),
        kind=MediaKind.TRACK,
        title=str(d.get("title") or "Unknown title"),
        subtitle=(str(d.get("artist")) if d.get("artist") else None),
        artwork=cover_url(d.get("coverArt") or d.get("albumId") or d.get("id")),
        playable=True,
        duration=_as_int(d.get("duration")),
    )


def _playlist_item(d: dict, cover_url: CoverUrl) -> MediaItem:
    return MediaItem(
        id=f"playlist:{d.get('id')}",
        kind=MediaKind.PLAYLIST,
        title=str(d.get("name") or "Playlist"),
        subtitle=_count_line(d.get("songCount"), "songs"),
        artwork=cover_url(d.get("coverArt")),
        browsable=True,
    )


def _genre_item(d: dict) -> MediaItem:
    name = str(d.get("value") or d.get("name") or "")
    return MediaItem(
        id=f"genre:{name}",
        kind=MediaKind.GENRE,
        title=name or "Genre",
        subtitle=_count_line(d.get("songCount"), "songs"),
        browsable=True,
    )


def parse_artists(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getArtists → flat artist list (the response groups them by index letter)."""
    index = (payload.get("artists") or {}).get("index") or []
    out: list[MediaItem] = []
    for group in index:
        for artist in group.get("artist") or []:
            out.append(_artist_item(artist, cover_url))
    return out


def parse_artist_albums(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getArtist → the artist's albums."""
    albums = (payload.get("artist") or {}).get("album") or []
    return [_album_item(a, cover_url) for a in albums]


def parse_album_songs(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getAlbum → the album's tracks (playable)."""
    songs = (payload.get("album") or {}).get("song") or []
    return [_song_item(s, cover_url) for s in songs]


def parse_album_list(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getAlbumList2 → an album shelf."""
    albums = (payload.get("albumList2") or {}).get("album") or []
    return [_album_item(a, cover_url) for a in albums]


def parse_playlists(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getPlaylists → the user's playlists."""
    playlists = (payload.get("playlists") or {}).get("playlist") or []
    return [_playlist_item(p, cover_url) for p in playlists]


def parse_playlist_songs(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getPlaylist → the playlist's tracks (playable)."""
    entries = (payload.get("playlist") or {}).get("entry") or []
    return [_song_item(s, cover_url) for s in entries]


def parse_genres(payload: dict) -> list[MediaItem]:
    """getGenres → genre containers."""
    genres = (payload.get("genres") or {}).get("genre") or []
    return [_genre_item(g) for g in genres]


def parse_songs_by_genre(payload: dict, cover_url: CoverUrl) -> list[MediaItem]:
    """getSongsByGenre → tracks in a genre (playable)."""
    songs = (payload.get("songsByGenre") or {}).get("song") or []
    return [_song_item(s, cover_url) for s in songs]


def parse_search3(payload: dict, cover_url: CoverUrl) -> SearchResults:
    """search3 → grouped hits."""
    res = payload.get("searchResult3") or {}
    return SearchResults(
        query="",
        artists=[_artist_item(a, cover_url) for a in (res.get("artist") or [])],
        albums=[_album_item(a, cover_url) for a in (res.get("album") or [])],
        tracks=[_song_item(s, cover_url) for s in (res.get("song") or [])],
    )


def _root_items() -> list[MediaItem]:
    """The static top-level browse categories (pure)."""
    items = [
        MediaItem(id="artists", kind=MediaKind.DIRECTORY, title="Artists", browsable=True)
    ]
    for node_id, title, _kind in _ALBUM_LISTS:
        items.append(
            MediaItem(id=node_id, kind=MediaKind.DIRECTORY, title=title, browsable=True)
        )
    items.append(
        MediaItem(id="playlists", kind=MediaKind.DIRECTORY, title="Playlists", browsable=True)
    )
    items.append(
        MediaItem(id="genres", kind=MediaKind.DIRECTORY, title="Genres", browsable=True)
    )
    return items


# --- the backend ----------------------------------------------------------

class SubsonicBackend(LibraryBackend):
    """Browse/search a Navidrome/OpenSubsonic server directly over its REST API."""

    id = "subsonic"
    name = "Navidrome / OpenSubsonic"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._base = base_url
        self._user = username
        self._password = password

    def _cover_url(self, cover_id: object) -> str | None:
        if not cover_id:
            return None
        return subsonic_cover_art_url(
            self._base, self._user, self._password, str(cover_id), size=300
        )

    def stream_url(self, item_id: str) -> str | None:
        """A directly-decodable ``/rest/stream`` URL for a track id."""
        return subsonic_stream_url(self._base, self._user, self._password, item_id)

    async def resolve_stream_url(self, item_id: str) -> str | None:
        return self.stream_url(item_id)

    async def play_spec(self, item_id: str) -> tuple[str, str]:
        # Direct mode: hand the player a ready-to-decode /rest/stream URL so the
        # token never rides in the browser and any player that can open a URL
        # (incl. MA players) can play it.
        url = self.stream_url(item_id)
        if not url:
            raise BackendError(
                "Could not build a stream URL — check the Navidrome/OpenSubsonic "
                "URL and login in the integration options."
            )
        return url, "music"

    async def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """GET a Subsonic API endpoint and return its ``subsonic-response`` body."""
        url = subsonic_rest_url(
            self._base, endpoint, params or {}, self._user, self._password, fmt="json"
        )
        try:
            async with self._session.get(url, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                # Navidrome serves JSON as application/json, but some servers use
                # text/…; don't let aiohttp reject on content-type.
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise BackendError(f"Subsonic {endpoint} request failed: {err}") from err
        except (ValueError, TypeError) as err:
            raise BackendError(f"Subsonic {endpoint} returned invalid JSON: {err}") from err
        payload = (data or {}).get("subsonic-response") or {}
        if payload.get("status") == "failed":
            err = payload.get("error") or {}
            raise BackendError(
                f"Subsonic error {err.get('code')}: {err.get('message') or 'unknown'}"
            )
        return payload

    async def browse(self, node_id: str | None = None) -> BrowseNode:
        node_id = node_id or "root"

        if node_id == "root":
            return BrowseNode(id="root", title=self.name, items=_root_items())

        if node_id == "artists":
            payload = await self._get("getArtists")
            return BrowseNode(
                id=node_id, title="Artists", parent="root",
                items=parse_artists(payload, self._cover_url),
            )

        if node_id.startswith("artist:"):
            payload = await self._get("getArtist", {"id": node_id.split(":", 1)[1]})
            title = str((payload.get("artist") or {}).get("name") or "Artist")
            return BrowseNode(
                id=node_id, title=title, parent="artists",
                items=parse_artist_albums(payload, self._cover_url),
            )

        if node_id.startswith("album:"):
            payload = await self._get("getAlbum", {"id": node_id.split(":", 1)[1]})
            title = str((payload.get("album") or {}).get("name") or "Album")
            return BrowseNode(
                id=node_id, title=title, parent="root", can_play_all=True,
                items=parse_album_songs(payload, self._cover_url),
            )

        if node_id in _ALBUM_LIST_TYPES:
            payload = await self._get(
                "getAlbumList2", {"type": _ALBUM_LIST_TYPES[node_id], "size": 100}
            )
            return BrowseNode(
                id=node_id, title=_ALBUM_LIST_TITLES[node_id], parent="root",
                items=parse_album_list(payload, self._cover_url),
            )

        if node_id == "playlists":
            payload = await self._get("getPlaylists")
            return BrowseNode(
                id=node_id, title="Playlists", parent="root",
                items=parse_playlists(payload, self._cover_url),
            )

        if node_id.startswith("playlist:"):
            payload = await self._get("getPlaylist", {"id": node_id.split(":", 1)[1]})
            title = str((payload.get("playlist") or {}).get("name") or "Playlist")
            return BrowseNode(
                id=node_id, title=title, parent="playlists", can_play_all=True,
                items=parse_playlist_songs(payload, self._cover_url),
            )

        if node_id == "genres":
            payload = await self._get("getGenres")
            return BrowseNode(
                id=node_id, title="Genres", parent="root", items=parse_genres(payload)
            )

        if node_id.startswith("genre:"):
            genre = node_id.split(":", 1)[1]
            payload = await self._get("getSongsByGenre", {"genre": genre, "count": 200})
            return BrowseNode(
                id=node_id, title=genre, parent="genres", can_play_all=True,
                items=parse_songs_by_genre(payload, self._cover_url),
            )

        raise BackendError(f"Unknown browse node {node_id!r}")

    async def search(self, query: str, *, limit: int = 30) -> SearchResults:
        payload = await self._get(
            "search3",
            {
                "query": query,
                "artistCount": limit,
                "albumCount": limit,
                "songCount": limit,
            },
        )
        results = parse_search3(payload, self._cover_url)
        results.query = query
        return results
