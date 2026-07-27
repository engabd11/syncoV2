"""Direct OpenSubsonic/Navidrome backend: URL building + pure response parsing.

No network: only the REST URL builders and the JSON→MediaItem parsers are
exercised (``SubsonicBackend._get`` is the sole network method and is not
touched here).
"""

from __future__ import annotations

import hashlib

from hue_music_sync.audio.subsonic import (
    subsonic_auth_params,
    subsonic_cover_art_url,
    subsonic_rest_url,
    subsonic_stream_url,
)
from hue_music_sync.library.base import MediaKind
from hue_music_sync.library.subsonic_backend import (
    _root_items,
    parse_album_list,
    parse_album_songs,
    parse_artist_albums,
    parse_artists,
    parse_genres,
    parse_playlists,
    parse_playlist_songs,
    parse_search3,
    parse_songs_by_genre,
)


def _cover(cover_id):
    return f"COVER:{cover_id}" if cover_id else None


# --- URL / auth builders --------------------------------------------------

def test_auth_params_hide_password_and_hash_with_salt():
    params = subsonic_auth_params("alice", "secret", salt="abcd")
    assert params["u"] == "alice"
    assert params["s"] == "abcd"
    assert params["t"] == hashlib.md5(b"secretabcd").hexdigest()  # noqa: S324
    assert all("secret" not in str(v) for v in params.values())


def test_rest_url_appends_endpoint_suffix_and_json_format():
    url = subsonic_rest_url("http://nas:4533", "getArtists", {}, "u", "p", salt="s")
    assert url.startswith("http://nas:4533/rest/getArtists.view?")
    assert "f=json" in url
    assert "u=u" in url


def test_rest_url_defaults_https_for_schemeless_host():
    url = subsonic_rest_url("nas/", "getGenres", {}, "u", "p", salt="s")
    assert url.startswith("https://nas/rest/getGenres.view?")


def test_stream_url_carries_no_json_format_and_hides_password():
    url = subsonic_stream_url("http://nas", "u", "secret", "T", salt="s")
    assert url.startswith("http://nas/rest/stream.view?")
    assert "id=T" in url
    assert "f=json" not in url  # audio, not JSON
    assert "secret" not in url


def test_cover_art_url_includes_size_and_none_when_missing():
    url = subsonic_cover_art_url("http://nas", "u", "p", "cid", size=300, salt="s")
    assert url.startswith("http://nas/rest/getCoverArt.view?")
    assert "id=cid" in url and "size=300" in url and "f=json" not in url
    assert subsonic_cover_art_url("http://nas", "u", "p", "", salt="s") is None


# --- browse tree / parsers ------------------------------------------------

def test_root_items_are_browsable_categories():
    items = _root_items()
    ids = {i.id for i in items}
    assert {"artists", "playlists", "genres"} <= ids
    assert any(i.id == "albums:newest" for i in items)
    assert all(i.browsable and not i.playable for i in items)


def test_parse_artists_flattens_index_groups():
    payload = {
        "artists": {
            "index": [
                {"name": "A", "artist": [
                    {"id": "1", "name": "ABBA", "albumCount": 3, "coverArt": "ar-1"}
                ]},
                {"name": "B", "artist": [{"id": "2", "name": "Beatles", "albumCount": 12}]},
            ]
        }
    }
    items = parse_artists(payload, _cover)
    assert [i.title for i in items] == ["ABBA", "Beatles"]
    assert items[0].id == "artist:1"
    assert items[0].kind == MediaKind.ARTIST and items[0].browsable
    assert items[0].subtitle == "3 albums"
    assert items[0].artwork == "COVER:ar-1"


def test_parse_album_songs_are_playable_tracks_with_raw_ids():
    payload = {"album": {"name": "Gold", "song": [
        {"id": "s1", "title": "SOS", "artist": "ABBA", "duration": 200, "coverArt": "c1"},
    ]}}
    items = parse_album_songs(payload, _cover)
    assert items[0].id == "s1"  # raw id → streamable
    assert items[0].playable and not items[0].browsable
    assert items[0].kind == MediaKind.TRACK
    assert items[0].duration == 200
    assert items[0].artwork == "COVER:c1"


def test_parse_artist_albums_and_album_list_prefix_ids():
    a = parse_artist_albums(
        {"artist": {"album": [{"id": "al1", "name": "Gold", "artist": "ABBA"}]}}, _cover
    )
    assert a[0].id == "album:al1" and a[0].browsable and a[0].kind == MediaKind.ALBUM
    b = parse_album_list(
        {"albumList2": {"album": [{"id": "al2", "name": "Live", "artist": "X"}]}}, _cover
    )
    assert b[0].id == "album:al2"


def test_parse_playlists_and_playlist_songs():
    items = parse_playlists(
        {"playlists": {"playlist": [{"id": "p1", "name": "Chill", "songCount": 20}]}}, _cover
    )
    assert items[0].id == "playlist:p1" and items[0].subtitle == "20 songs"
    songs = parse_playlist_songs(
        {"playlist": {"entry": [{"id": "s9", "title": "T", "artist": "A"}]}}, _cover
    )
    assert songs[0].id == "s9" and songs[0].playable


def test_parse_genres_and_songs_by_genre():
    g = parse_genres({"genres": {"genre": [{"value": "Rock", "songCount": 100}]}})
    assert g[0].id == "genre:Rock" and g[0].kind == MediaKind.GENRE
    assert g[0].subtitle == "100 songs"
    songs = parse_songs_by_genre(
        {"songsByGenre": {"song": [{"id": "s5", "title": "Riff", "artist": "Band"}]}}, _cover
    )
    assert songs[0].id == "s5" and songs[0].playable


def test_parse_search3_groups_hits():
    payload = {"searchResult3": {
        "artist": [{"id": "1", "name": "ABBA"}],
        "album": [{"id": "al1", "name": "Gold", "artist": "ABBA"}],
        "song": [{"id": "s1", "title": "SOS", "artist": "ABBA", "duration": 200}],
    }}
    res = parse_search3(payload, _cover)
    assert len(res.artists) == 1 and len(res.albums) == 1 and len(res.tracks) == 1
    assert res.artists[0].id == "artist:1"
    assert res.albums[0].id == "album:al1"
    assert res.tracks[0].id == "s1" and res.tracks[0].playable


def test_parse_handles_empty_payloads_gracefully():
    assert parse_artists({}, _cover) == []
    assert parse_album_songs({}, _cover) == []
    r = parse_search3({}, _cover)
    assert r.artists == [] and r.albums == [] and r.tracks == []
