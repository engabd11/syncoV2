"""Music Assistant library backend.

Browse **delegates to Music Assistant's own browse tree** — the MA media_player
entity's ``async_browse_media`` — translated into our backend-agnostic
:class:`BrowseNode`. That way we inherit MA's correct, version-matched library
structure, its proxied artwork thumbnails and its per-item playability, instead
of reimplementing browse on the fragile ``mass.music.*`` internals. Free-text
search uses the MA client's ``music.search`` (best-effort; empty on any client
mismatch — browse still works).

HA-coupled (imports Home Assistant); not exercised by the pure ``tests/`` suite,
so every access is defensive across MA/HA versions.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .base import (
    BackendError,
    BrowseNode,
    LibraryBackend,
    MediaItem,
    MediaKind,
    SearchResults,
)

_LOGGER = logging.getLogger(__name__)

# One node-id string encodes MA's (media_content_type, media_content_id) pair.
# U+0001 (a control char) can never appear in a content id/type, so it is a safe
# separator that round-trips through the card's JSON untouched.
_SEP = chr(1)

_KIND_BY_TYPE: dict[str, MediaKind] = {
    "artist": MediaKind.ARTIST,
    "album": MediaKind.ALBUM,
    "track": MediaKind.TRACK,
    "playlist": MediaKind.PLAYLIST,
    "radio": MediaKind.RADIO,
}


def _encode(content_type: object, content_id: object) -> str:
    return f"{content_type or ''}{_SEP}{content_id or ''}"


def _decode(node_id: str | None) -> tuple[str | None, str | None]:
    if not node_id or node_id == "root":
        return None, None
    if _SEP in node_id:
        ctype, cid = node_id.split(_SEP, 1)
        return (ctype or None), (cid or None)
    return None, node_id


def _kind_for(content_type: object, can_expand: bool) -> MediaKind:
    key = str(content_type or "").lower()
    for name, kind in _KIND_BY_TYPE.items():
        if name in key:
            return kind
    return MediaKind.DIRECTORY if can_expand else MediaKind.TRACK


class MABackend(LibraryBackend):
    """Browse/search the Music Assistant library through MA's own browse tree."""

    id = "music_assistant"
    name = "Music Assistant"

    def __init__(self, hass: HomeAssistant, browse_entity_id: str | None = None) -> None:
        self._hass = hass
        # Any MA media_player entity returns the same library; prefer the one the
        # area is following so the browse root matches the active player.
        self._browse_entity_id = browse_entity_id

    # --- MA entity plumbing ---------------------------------------------

    def _browse_entity_candidate(self) -> str | None:
        from ..audio.source import ma_player_provider

        if self._browse_entity_id and self._hass.states.get(self._browse_entity_id):
            if ma_player_provider(self._hass, self._browse_entity_id):
                return self._browse_entity_id
        for state in self._hass.states.async_all("media_player"):
            if ma_player_provider(self._hass, state.entity_id):
                return state.entity_id
        return None

    def _entity(self, entity_id: str):
        # media_player's EntityComponent lives under hass.data["media_player"]
        # (a HassKey that compares equal to its domain string on every HA version
        # that has shipped this integration's minimum).
        component = self._hass.data.get("media_player")
        getter = getattr(component, "get_entity", None)
        return getter(entity_id) if callable(getter) else None

    # --- LibraryBackend --------------------------------------------------

    async def browse(self, node_id: str | None = None) -> BrowseNode:
        entity_id = self._browse_entity_candidate()
        if not entity_id:
            raise BackendError(
                "No Music Assistant player is available to browse. Start Music "
                "Assistant (or a Sendspin player), or switch the library backend "
                "to Navidrome in the integration options."
            )
        entity = self._entity(entity_id)
        if entity is None or not hasattr(entity, "async_browse_media"):
            raise BackendError(
                "This Music Assistant player cannot browse media on this Home "
                "Assistant version."
            )
        content_type, content_id = _decode(node_id)
        try:
            result = await entity.async_browse_media(content_type, content_id)
        except Exception as err:  # noqa: BLE001 - MA/HA browse surface varies
            raise BackendError(f"Music Assistant browse failed: {err}") from err

        items: list[MediaItem] = []
        for child in getattr(result, "children", None) or []:
            c_type = getattr(child, "media_content_type", None)
            c_id = getattr(child, "media_content_id", None)
            can_play = bool(getattr(child, "can_play", False))
            can_expand = bool(getattr(child, "can_expand", False))
            items.append(
                MediaItem(
                    id=_encode(c_type, c_id),
                    kind=_kind_for(c_type, can_expand),
                    title=str(getattr(child, "title", None) or "?"),
                    artwork=getattr(child, "thumbnail", None) or None,
                    playable=can_play,
                    browsable=can_expand,
                    provider=(str(c_type) if c_type else None),
                )
            )
        return BrowseNode(
            id=node_id or "root",
            title=str(getattr(result, "title", None) or self.name),
            items=items,
            can_play_all=bool(getattr(result, "can_play", False)),
        )

    async def search(self, query: str, *, limit: int = 30) -> SearchResults:
        from ..audio.source import _find_mass_client

        results = SearchResults(query=query)
        mass = _find_mass_client(self._hass)
        music = getattr(mass, "music", None) if mass is not None else None
        search = getattr(music, "search", None)
        if not callable(search):
            return results
        try:
            res = await search(search_query=query, limit=limit)
        except TypeError:
            try:
                res = await search(query)
            except Exception as err:  # noqa: BLE001 - defensive across MA versions
                _LOGGER.debug("MA search failed: %s", err)
                return results
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("MA search failed: %s", err)
            return results
        results.artists = [
            self._search_item(a, MediaKind.ARTIST)
            for a in (getattr(res, "artists", None) or [])
        ]
        results.albums = [
            self._search_item(a, MediaKind.ALBUM)
            for a in (getattr(res, "albums", None) or [])
        ]
        results.tracks = [
            self._search_item(t, MediaKind.TRACK)
            for t in (getattr(res, "tracks", None) or [])
        ]
        return results

    @staticmethod
    def _search_item(media_item, kind: MediaKind) -> MediaItem:
        from ..audio.source import _media_item_artist

        uri = getattr(media_item, "uri", None) or ""
        content_type = {
            MediaKind.ARTIST: "artist",
            MediaKind.ALBUM: "album",
            MediaKind.TRACK: "track",
        }[kind]
        return MediaItem(
            id=_encode(content_type, uri),
            kind=kind,
            title=str(getattr(media_item, "name", None) or "?"),
            subtitle=_media_item_artist(media_item),
            playable=(kind == MediaKind.TRACK),
            browsable=(kind != MediaKind.TRACK),
            provider=content_type,
        )

    async def play_spec(self, item_id: str) -> tuple[str, str]:
        content_type, content_id = _decode(item_id)
        if not content_id:
            raise BackendError("Nothing to play.")
        return content_id, (content_type or "music")
