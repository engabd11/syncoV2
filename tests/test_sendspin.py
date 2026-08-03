"""Sendspin wire decoding and URL resolution.

Pure message handling — no socket. Fixtures here match the shapes the probe
script records (``scripts/spike_sendspin.py --record``), so a protocol change
shows up as a test failure rather than a silently dead clock feed.
"""

from __future__ import annotations

import pytest

from hue_music_sync.audio.sendspin import (
    DEFAULT_PATH,
    DEFAULT_PORT,
    parse_progress,
    server_url,
)


# -- URL resolution -----------------------------------------------------------


def test_url_from_music_assistant_base_url():
    """The Sendspin server shares MA's host but has its own port."""
    assert server_url("http://192.168.0.50:8095") == (
        f"ws://192.168.0.50:{DEFAULT_PORT}{DEFAULT_PATH}"
    )


def test_url_from_bare_host_override():
    assert server_url(None, "192.168.0.50") == (
        f"ws://192.168.0.50:{DEFAULT_PORT}{DEFAULT_PATH}"
    )


def test_url_override_keeps_an_explicit_port():
    assert server_url(None, "mediabox.local:9999") == (
        f"ws://mediabox.local:9999{DEFAULT_PATH}"
    )


def test_url_override_accepts_a_full_ws_url():
    url = "wss://mediabox.example.com/sendspin"
    assert server_url("http://ignored:8095", url) == url


def test_url_override_wins_over_the_base_url():
    assert server_url("http://192.168.0.50:8095", "10.0.0.9").startswith("ws://10.0.0.9:")


def test_url_is_none_without_anything_to_go_on():
    assert server_url(None, None) is None
    assert server_url("", "") is None
    assert server_url("not a url", None) is None


# -- progress decoding --------------------------------------------------------


def _state(**progress):
    payload = {
        "metadata": {
            "timestamp": 1_700_000_000_000_000,
            "title": "Digital Love",
            "artist": "Daft Punk",
            "progress": {
                "track_progress": 61_000,
                "track_duration": 301_000,
                "playback_speed": 1000,
                **progress,
            },
        }
    }
    return payload


def test_parse_progress_converts_units():
    """ms -> s, and playback_speed's x1000 multiplier -> a plain rate."""
    p = parse_progress(_state())
    assert p is not None
    assert p.position_s == pytest.approx(61.0)
    assert p.duration_s == pytest.approx(301.0)
    assert p.rate == pytest.approx(1.0)
    assert p.server_us == pytest.approx(1_700_000_000_000_000)
    assert p.title == "Digital Love"
    assert p.artist == "Daft Punk"


def test_parse_progress_paused_is_rate_zero():
    p = parse_progress(_state(playback_speed=0))
    assert p is not None and p.rate == 0.0


def test_parse_progress_honours_a_non_unit_speed():
    p = parse_progress(_state(playback_speed=1250))
    assert p is not None and p.rate == pytest.approx(1.25)


def test_parse_progress_tolerates_unknown_duration():
    """`track_duration` is 0 for live/unknown streams, which is not an error."""
    p = parse_progress(_state(track_duration=0))
    assert p is not None and p.duration_s == 0.0


def test_parse_progress_needs_a_progress_object():
    """Metadata-only updates (artwork, title) carry no anchor."""
    assert parse_progress({"metadata": {"timestamp": 1, "title": "x"}}) is None


def test_parse_progress_needs_a_timestamp():
    """Without the server time it is valid at, an anchor cannot be placed."""
    payload = _state()
    del payload["metadata"]["timestamp"]
    assert parse_progress(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"metadata": None},
        {"metadata": {}},
        {"controller": {"volume": 50}},
        {"metadata": {"timestamp": 1, "progress": "not-an-object"}},
        {"metadata": {"timestamp": "nonsense", "progress": {"track_progress": 1}}},
        {"metadata": {"timestamp": 1, "progress": {"track_progress": "x"}}},
    ],
)
def test_parse_progress_survives_malformed_payloads(payload):
    """A partial or unexpected payload must never raise into the socket loop."""
    assert parse_progress(payload) is None


# -- the anchor as the clock sees it ------------------------------------------


def test_progress_projects_the_playhead():
    """The whole point: position is exact between reports, given the clock.

    ``track_progress + (server_now - timestamp) x rate`` is the projection the
    light show runs on, so pin the arithmetic.
    """
    p = parse_progress(_state(track_progress=61_000, playback_speed=1000))
    later_us = p.server_us + 7_500_000  # 7.5 s later on the server clock
    projected = p.position_s + (later_us - p.server_us) / 1e6 * p.rate
    assert projected == pytest.approx(68.5)
