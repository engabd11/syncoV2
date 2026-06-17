"""OpenSubsonic/Navidrome stream-URL building (token auth)."""

from __future__ import annotations

import hashlib

from hue_music_sync.audio.subsonic import is_subsonic_provider, subsonic_stream_url


def test_is_subsonic_provider():
    assert is_subsonic_provider("opensubsonic--2tqKHCzo")
    assert is_subsonic_provider("subsonic")
    assert not is_subsonic_provider("spotify")
    assert not is_subsonic_provider("")
    assert not is_subsonic_provider(None)


def test_stream_url_uses_token_auth_and_hides_password():
    url = subsonic_stream_url("http://nas:4533", "alice", "secret", "TRACKID", salt="abcd")
    token = hashlib.md5(b"secretabcd").hexdigest()  # noqa: S324 - Subsonic spec
    assert url.startswith("http://nas:4533/rest/stream.view?")
    assert "id=TRACKID" in url
    assert "u=alice" in url
    assert f"t={token}" in url
    assert "s=abcd" in url
    assert "secret" not in url  # password is never placed on the URL


def test_stream_url_adds_http_scheme_and_strips_trailing_slash():
    url = subsonic_stream_url("nas:4533/", "u", "p", "x", salt="s")
    assert url.startswith("http://nas:4533/rest/stream.view?")


def test_stream_url_none_when_a_field_is_missing():
    assert subsonic_stream_url("", "u", "p", "x") is None
    assert subsonic_stream_url("http://x", "u", "p", "") is None
    assert subsonic_stream_url("http://x", "", "p", "id") is None
    assert subsonic_stream_url("http://x", "u", "", "id") is None
