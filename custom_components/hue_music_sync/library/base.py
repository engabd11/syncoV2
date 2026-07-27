"""Backend-agnostic music library abstraction.

Everything the Synco player needs to *browse* and *search* a music source flows
through :class:`LibraryBackend`, so a Music Assistant backend and a direct
OpenSubsonic/Navidrome backend are interchangeable. Playback *transport*
(play/pause/seek/volume/group) is the responsibility of the ``media_player``
entity, not this layer — a backend only resolves what to play and, when it can,
a directly-decodable URL for it.

Pure module (no Home Assistant imports) so the dataclasses and any node-shaping
helpers are unit-testable on their own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum


class MediaKind(StrEnum):
    """What a browse item is — drives the card's icon and tap behaviour."""

    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    GENRE = "genre"
    RADIO = "radio"
    DIRECTORY = "directory"  # a category/container with no media of its own


@dataclass(slots=True)
class MediaItem:
    """One browsable or playable thing (artist, album, track, playlist, …)."""

    id: str
    kind: MediaKind
    title: str
    subtitle: str | None = None  # artist, owner, or an "N songs" line
    artwork: str | None = None  # ready-to-use image URL, or None
    playable: bool = False  # can be enqueued / played directly
    browsable: bool = False  # has children — browse into it
    duration: int | None = None  # seconds, tracks only
    provider: str | None = None  # opaque backend hint (e.g. an MA provider id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": str(self.kind),
            "title": self.title,
            "subtitle": self.subtitle,
            "artwork": self.artwork,
            "playable": self.playable,
            "browsable": self.browsable,
            "duration": self.duration,
            "provider": self.provider,
        }


@dataclass(slots=True)
class BrowseNode:
    """A browse level: a titled list of items, with a back-pointer to its parent."""

    id: str
    title: str
    items: list[MediaItem] = field(default_factory=list)
    parent: str | None = None
    # True when the whole node plays as a set (an album/playlist), so the card
    # can offer a single "play all" action.
    can_play_all: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "parent": self.parent,
            "can_play_all": self.can_play_all,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass(slots=True)
class SearchResults:
    """Grouped full-text search hits."""

    query: str
    artists: list[MediaItem] = field(default_factory=list)
    albums: list[MediaItem] = field(default_factory=list)
    tracks: list[MediaItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "artists": [i.to_dict() for i in self.artists],
            "albums": [i.to_dict() for i in self.albums],
            "tracks": [i.to_dict() for i in self.tracks],
        }


class BackendError(RuntimeError):
    """A backend could not satisfy a request (network, auth, unavailable)."""


class LibraryBackend(ABC):
    """Browse / search / resolve for one music source. One is active at a time."""

    #: Stable id used in the options + the card's backend switch.
    id: str = "base"
    #: Human label for the UI.
    name: str = "Library"

    @abstractmethod
    async def browse(self, node_id: str | None = None) -> BrowseNode:
        """Return the children of ``node_id`` (the root level when ``None``)."""

    @abstractmethod
    async def search(self, query: str, *, limit: int = 30) -> SearchResults:
        """Full-text search across artists, albums and tracks."""

    async def resolve_stream_url(self, item_id: str) -> str | None:
        """A directly-decodable audio URL for ``item_id`` when the backend can
        build one (direct-play / bit-perfect paths). Default ``None``: the
        backend hands the id to a player and lets it resolve the stream."""
        return None

    async def play_spec(self, item_id: str) -> tuple[str, str]:
        """``(media_content_id, media_content_type)`` to hand to HA's
        ``media_player.play_media`` for this item. Default: play the id as a
        generic music content id. Backends override to resolve a stream URL or
        translate their own id scheme."""
        return item_id, "music"

    async def close(self) -> None:
        """Release any resources held by the backend. Default: no-op."""
