"""The backend-agnostic library dataclasses (pure, no HA)."""

from __future__ import annotations

from hue_music_sync.library.base import BrowseNode, MediaItem, MediaKind, SearchResults


def test_media_item_to_dict_round_trips_fields():
    item = MediaItem(
        id="s1",
        kind=MediaKind.TRACK,
        title="SOS",
        subtitle="ABBA",
        artwork="http://x/art",
        playable=True,
        duration=200,
    )
    d = item.to_dict()
    assert d["id"] == "s1"
    assert d["kind"] == "track"  # StrEnum serialises to its value
    assert d["playable"] is True
    assert d["browsable"] is False
    assert d["duration"] == 200


def test_browse_node_to_dict_nests_items():
    node = BrowseNode(
        id="album:1",
        title="Gold",
        parent="root",
        can_play_all=True,
        items=[MediaItem(id="s1", kind=MediaKind.TRACK, title="One", playable=True)],
    )
    d = node.to_dict()
    assert d["id"] == "album:1"
    assert d["parent"] == "root"
    assert d["can_play_all"] is True
    assert len(d["items"]) == 1
    assert d["items"][0]["title"] == "One"


def test_search_results_to_dict_groups_hits():
    res = SearchResults(
        query="abba",
        artists=[MediaItem(id="artist:1", kind=MediaKind.ARTIST, title="ABBA", browsable=True)],
        tracks=[MediaItem(id="s1", kind=MediaKind.TRACK, title="SOS", playable=True)],
    )
    d = res.to_dict()
    assert d["query"] == "abba"
    assert len(d["artists"]) == 1 and len(d["tracks"]) == 1 and d["albums"] == []
