"""Music Assistant library backend.

Presents a proper music-library tree — **Artists / Albums / Tracks / Playlists**,
drilling artist → albums → tracks — built from the Music Assistant client's music
controller (``mass.music.*``), so the card browses like a music player rather than
a file tree. Free-text search uses ``mass.music.search``.

Node ids are small JSON blobs (opaque to the card) carrying exactly what the next
call needs: a category name, a ``provider`` + ``item_id`` for a container, or a
track ``uri`` to play.

HA-coupled; defensive across MA client versions (mirrors the ``getattr`` /
``as_track_list`` patterns already used in ``audio/source.py``).
"""

from __future__ import annotations

import json
import logging

from homeassistant.core import HomeAssistant

from ..audio.ma_stream import as_track_list
from .base import (
    BackendError,
    BrowseNode,
    LibraryBackend,
    MediaItem,
    MediaKind,
    SearchResults,
)

_LOGGER = logging.getLogger(__name__)

# Top-level categories (id "c" -> label).
_CATEGORIES = (
    ("artists", "Artists"),
    ("albums", "Albums"),
    ("tracks", "Tracks"),
    ("playlists", "Playlists"),
)


# --- node-id (JSON) helpers ----------------------------------------------

def _nid(**data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True)


def _parse_nid(node_id: str | None) -> dict:
    if not node_id or node_id == "root":
        return {}
    try:
        parsed = json.loads(node_id)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


# --- MA media-item -> MediaItem ------------------------------------------

def _artist_str(item) -> str | None:
    from ..audio.source import _media_item_artist

    return _media_item_artist(item)


def _image_url(item) -> str | None:
    """A directly-usable http artwork URL, or None.

    MA serves most library images through its own (signed) imageproxy, which we
    don't reconstruct here; we only use an image the item exposes as a
    remotely-accessible http URL. Missing artwork is fine — the card shows a
    kind glyph. (Richer thumbnails come with the design pass.)
    """
    img = getattr(item, "image", None)
    if img is None:
        return None
    if isinstance(img, str):
        return img if img.startswith(("http://", "https://")) else None
    path = getattr(img, "path", None)
    if getattr(img, "remotely_accessible", False) and isinstance(path, str) \
            and path.startswith(("http://", "https://")):
        return path
    return None


def _provider(item) -> str:
    return str(getattr(item, "provider", "") or "")


def _item_id(item) -> str:
    return str(getattr(item, "item_id", "") or "")


def _artist_item(item) -> MediaItem:
    return MediaItem(
        id=_nid(t="artist", p=_provider(item), i=_item_id(item)),
        kind=MediaKind.ARTIST,
        title=str(getattr(item, "name", None) or "?"),
        artwork=_image_url(item),
        browsable=True,
        provider=_provider(item) or None,
    )


def _album_item(item) -> MediaItem:
    return MediaItem(
        id=_nid(t="album", p=_provider(item), i=_item_id(item)),
        kind=MediaKind.ALBUM,
        title=str(getattr(item, "name", None) or "?"),
        subtitle=_artist_str(item),
        artwork=_image_url(item),
        browsable=True,
    )


def _playlist_item(item) -> MediaItem:
    return MediaItem(
        id=_nid(t="playlist", p=_provider(item), i=_item_id(item)),
        kind=MediaKind.PLAYLIST,
        title=str(getattr(item, "name", None) or "?"),
        artwork=_image_url(item),
        browsable=True,
    )


def _track_item(item) -> MediaItem:
    uri = str(getattr(item, "uri", "") or "")
    dur = getattr(item, "duration", None)
    return MediaItem(
        id=_nid(t="track", u=uri),
        kind=MediaKind.TRACK,
        title=str(getattr(item, "name", None) or "?"),
        subtitle=_artist_str(item),
        artwork=_image_url(item),
        playable=bool(uri),
        duration=int(dur) if isinstance(dur, (int, float)) else None,
    )


class MABackend(LibraryBackend):
    """Browse/search the Music Assistant library as an Artists/Albums/Tracks tree."""

    id = "music_assistant"
    name = "Music Assistant"

    def __init__(self, hass: HomeAssistant, browse_entity_id: str | None = None) -> None:
        self._hass = hass
        self._browse_entity_id = browse_entity_id  # kept for factory signature compat

    def _music(self):
        from ..audio.source import _find_mass_client

        mass = _find_mass_client(self._hass)
        music = getattr(mass, "music", None) if mass is not None else None
        if music is None:
            raise BackendError(
                "Music Assistant is not available. Start it, or switch the library "
                "backend to Navidrome in the integration options."
            )
        return music

    async def _call(self, music, name: str, *args, **kwargs):
        """Call ``mass.music.<name>`` defensively across client versions."""
        fn = getattr(music, name, None)
        if not callable(fn):
            raise BackendError(f"This Music Assistant version has no {name}().")
        try:
            return await fn(*args, **kwargs)
        except TypeError:
            # A different signature (e.g. no limit/offset): retry positionally.
            try:
                return await fn(*args)
            except Exception as err:  # noqa: BLE001 - defensive across MA versions
                raise BackendError(f"Music Assistant {name} failed: {err}") from err
        except BackendError:
            raise
        except Exception as err:  # noqa: BLE001
            raise BackendError(f"Music Assistant {name} failed: {err}") from err

    async def browse(self, node_id: str | None = None) -> BrowseNode:
        node = _parse_nid(node_id)
        kind = node.get("t")

        if not kind:  # root: the category shelves
            items = [
                MediaItem(id=_nid(t="cat", c=cid), kind=MediaKind.DIRECTORY,
                          title=label, browsable=True)
                for cid, label in _CATEGORIES
            ]
            return BrowseNode(id="root", title=self.name, items=items)

        music = self._music()

        if kind == "cat":
            cat = node.get("c")
            if cat == "artists":
                res = await self._call(music, "get_library_artists", limit=500, offset=0)
                return BrowseNode(id=node_id, title="Artists", parent="root",
                                  items=[_artist_item(x) for x in as_track_list(res)])
            if cat == "albums":
                res = await self._call(music, "get_library_albums", limit=500, offset=0)
                return BrowseNode(id=node_id, title="Albums", parent="root",
                                  items=[_album_item(x) for x in as_track_list(res)])
            if cat == "tracks":
                res = await self._call(music, "get_library_tracks", limit=500, offset=0)
                return BrowseNode(id=node_id, title="Tracks", parent="root",
                                  items=[_track_item(x) for x in as_track_list(res)])
            if cat == "playlists":
                res = await self._call(music, "get_library_playlists", limit=500, offset=0)
                return BrowseNode(id=node_id, title="Playlists", parent="root",
                                  items=[_playlist_item(x) for x in as_track_list(res)])
            raise BackendError(f"Unknown category {cat!r}")

        if kind == "artist":
            res = await self._call(music, "get_artist_albums", node.get("i"), node.get("p"))
            return BrowseNode(id=node_id, title="Albums",
                              parent=_nid(t="cat", c="artists"),
                              items=[_album_item(x) for x in as_track_list(res)])

        if kind == "album":
            res = await self._call(music, "get_album_tracks", node.get("i"), node.get("p"))
            return BrowseNode(id=node_id, title="Album", parent="root", can_play_all=True,
                              items=[_track_item(x) for x in as_track_list(res)])

        if kind == "playlist":
            res = await self._call(music, "get_playlist_tracks", node.get("i"), node.get("p"))
            return BrowseNode(id=node_id, title="Playlist",
                              parent=_nid(t="cat", c="playlists"), can_play_all=True,
                              items=[_track_item(x) for x in as_track_list(res)])

        raise BackendError("Unknown browse node.")

    async def search(self, query: str, *, limit: int = 30) -> SearchResults:
        music = self._music()
        results = SearchResults(query=query)
        try:
            res = await music.search(search_query=query, limit=limit)
        except TypeError:
            try:
                res = await music.search(query)
            except Exception as err:  # noqa: BLE001 - defensive across MA versions
                _LOGGER.debug("MA search failed: %s", err)
                return results
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("MA search failed: %s", err)
            return results
        results.artists = [_artist_item(x) for x in (getattr(res, "artists", None) or [])]
        results.albums = [_album_item(x) for x in (getattr(res, "albums", None) or [])]
        results.tracks = [_track_item(x) for x in (getattr(res, "tracks", None) or [])]
        return results

    async def play_spec(self, item_id: str) -> tuple[str, str]:
        node = _parse_nid(item_id)
        if node.get("t") == "track" and node.get("u"):
            return node["u"], "music"
        # A raw MA uri passed straight through (defensive).
        if item_id and "://" in item_id:
            return item_id, "music"
        raise BackendError("This item can't be played directly.")
