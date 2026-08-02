"""Tests for the CLIP v2 Server-Sent Events payload parser.

The connection itself needs a real bridge; what is worth locking down here is
the shape of what comes back. The bridge groups everything that changed inside a
one-second window into a single container, so "one event = one resource" is
exactly the assumption that would silently drop the ``entertainment_configuration``
update telling us the Hue app took the area.
"""

from __future__ import annotations

import json

from hue_music_sync.hue.events import parse_event_payload


def test_flattens_containers_into_resources():
    payload = json.dumps(
        [
            {
                "id": "1634576695:0",
                "type": "update",
                "data": [
                    {"id": "area-1", "type": "entertainment_configuration",
                     "status": "inactive"},
                    {"id": "light-9", "type": "light", "on": {"on": False}},
                ],
            }
        ]
    )
    resources = parse_event_payload(payload)
    assert [r["id"] for r in resources] == ["area-1", "light-9"]
    assert resources[0]["status"] == "inactive"


def test_flattens_multiple_containers():
    payload = json.dumps(
        [
            {"type": "update", "data": [{"id": "a", "type": "light"}]},
            {"type": "update", "data": [{"id": "b", "type": "light"}]},
        ]
    )
    assert [r["id"] for r in parse_event_payload(payload)] == ["a", "b"]


def test_active_streamer_change_survives_parsing():
    # Events carry only what changed, so an area handover can arrive as nothing
    # but the new active_streamer.
    payload = json.dumps(
        [{"data": [{"id": "area-1", "type": "entertainment_configuration",
                    "active_streamer": "other-app-id"}]}]
    )
    (resource,) = parse_event_payload(payload)
    assert resource["active_streamer"] == "other-app-id"
    assert "status" not in resource


def test_malformed_payload_is_ignored_not_raised():
    # A parser error must never be able to kill the subscription.
    assert parse_event_payload("not json at all") == []
    assert parse_event_payload("") == []


def test_non_list_and_missing_data_are_tolerated():
    assert parse_event_payload(json.dumps({"type": "update"})) == []
    assert parse_event_payload(json.dumps([{"type": "update"}])) == []
    assert parse_event_payload(json.dumps([{"data": None}])) == []
    assert parse_event_payload(json.dumps([{"data": ["not-a-dict"]}])) == []
