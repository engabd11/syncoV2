"""Tests for the CLIP v2 client's request shaping and rate discipline.

The bridge itself can't be exercised here, so these lock down the things the
Hue docs are prescriptive about: how many Zigbee messages a light command costs,
how fast light commands may be issued, and how gamuts are read off the API.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hue_music_sync.const import LIGHT_COMMAND_MIN_INTERVAL
from hue_music_sync.hue.bridge import (
    HueBridge,
    _gamuts_from_lights,
    capture_light_state,
    restore_light_body,
)


# --- light command shaping ------------------------------------------------

def test_restore_body_carries_on_by_default():
    state = {"id": "l1", "on": True, "brightness": 42.0, "xy": {"x": 0.3, "y": 0.3}}
    body = restore_light_body(state)
    assert body["on"] == {"on": True}
    assert body["dimming"] == {"brightness": 42.0}
    assert body["color"] == {"xy": {"x": 0.3, "y": 0.3}}


def test_restore_body_omits_redundant_on():
    # "There is no need to include an 'on' parameter in the hue API command when
    # a lamp is already (known to be) on" — each parameter is a separate Zigbee
    # message, so dropping it takes bri+xy+on (3 messages, 125 ms) down to
    # bri+xy (2 messages, 95 ms).
    state = {"id": "l1", "on": True, "brightness": 42.0, "xy": {"x": 0.3, "y": 0.3}}
    body = restore_light_body(state, assume_on=True)
    assert "on" not in body
    assert set(body) == {"dimming", "color"}


def test_restore_body_keeps_on_when_turning_a_light_off():
    state = {"id": "l1", "on": False, "brightness": 42.0}
    assert restore_light_body(state, assume_on=True)["on"] == {"on": False}


def test_restore_body_never_ends_up_empty():
    # A lamp that reports nothing but on/off still needs a command to restore.
    state = {"id": "l1", "on": True}
    assert restore_light_body(state, assume_on=True) == {"on": {"on": True}}


def test_restore_body_prefers_colour_temperature_over_xy():
    # "If you try and control multiple conflicting parameters at once… xy beats
    # ct", so a light captured in white mode must not also carry an xy.
    light = {
        "id": "l1",
        "on": {"on": True},
        "dimming": {"brightness": 80.0},
        "color_temperature": {"mirek": 300},
        "color": {"xy": {"x": 0.4, "y": 0.4}},
    }
    body = restore_light_body(capture_light_state(light))
    assert body["color_temperature"] == {"mirek": 300}
    assert "color" not in body


# --- pacing ---------------------------------------------------------------

def _bridge() -> HueBridge:
    return HueBridge(session=None, host="h", app_key="k", ssl_ctx=None)


def test_light_puts_are_paced_to_the_documented_rate():
    # "Stay at roughly 10 commands per second to the /lights resource with a
    # 100ms gap between each API call." An 8-lamp restore used to fire every PUT
    # back to back, which buffers inside the bridge and eventually gets dropped.
    bridge = _bridge()
    sent: list[float] = []

    async def _fake_put(path, body):
        sent.append(time.monotonic())

    bridge._put = _fake_put

    async def _run():
        for i in range(4):
            await bridge._put_light(f"l{i}", {"on": {"on": True}})

    started = time.monotonic()
    asyncio.run(_run())

    assert len(sent) == 4
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert all(g >= LIGHT_COMMAND_MIN_INTERVAL * 0.9 for g in gaps), gaps
    # Three gaps for four commands; the first goes out immediately.
    assert time.monotonic() - started >= LIGHT_COMMAND_MIN_INTERVAL * 2.7


def test_pacing_survives_a_failed_put():
    # A light that errors must still consume its slot, or one bad lamp lets the
    # rest of the restore burst through unpaced.
    bridge = _bridge()
    calls: list[float] = []

    async def _fake_put(path, body):
        calls.append(time.monotonic())
        raise OSError("nope")

    bridge._put = _fake_put

    async def _run():
        for i in range(3):
            with pytest.raises(OSError):
                await bridge._put_light(f"l{i}", {})

    asyncio.run(_run())
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(g >= LIGHT_COMMAND_MIN_INTERVAL * 0.9 for g in gaps), gaps


# --- gamut extraction -----------------------------------------------------

def test_gamuts_read_from_the_api_not_hardcoded():
    lights = [
        {
            "id": "l1",
            "color": {
                "gamut": {
                    "red": {"x": 0.6915, "y": 0.3038},
                    "green": {"x": 0.17, "y": 0.7},
                    "blue": {"x": 0.1532, "y": 0.0475},
                }
            },
        }
    ]
    assert _gamuts_from_lights(lights) == {
        "l1": ((0.6915, 0.3038), (0.17, 0.7), (0.1532, 0.0475))
    }


def test_gamutless_and_malformed_lights_are_skipped():
    # White-only lamps have no color object at all; they fall back to the
    # encoder's default rather than breaking the whole area's resolution.
    lights = [
        {"id": "white", "dimming": {"brightness": 50}},
        {"id": "nogamut", "color": {"xy": {"x": 0.3, "y": 0.3}}},
        {"id": "broken", "color": {"gamut": {"red": {"x": "nope"}}}},
    ]
    assert _gamuts_from_lights(lights) == {}


def test_parse_config_resolves_channel_gamuts_through_the_service_chain():
    # channel members -> entertainment service -> owner device -> light -> gamut
    gamut = ((0.7, 0.29), (0.21, 0.71), (0.13, 0.08))
    config = HueBridge._parse_config(
        {
            "id": "area-1",
            "metadata": {"name": "Lounge"},
            "status": "active",
            "configuration_type": "screen",
            "active_streamer": "app-123",
            "channels": [
                {
                    "channel_id": 0,
                    "position": {"x": -0.6, "y": 0.8, "z": 0.0},
                    "members": [{"service": {"rtype": "entertainment", "rid": "e1"}}],
                },
                {"channel_id": 1, "position": {"x": 0.6, "y": 0.8, "z": 0.0}},
            ],
        },
        ent_services={"e1": "dev1"},
        device_lights={"dev1": "light1"},
        light_gamuts={"light1": gamut},
    )
    assert config.name == "Lounge"
    assert config.configuration_type == "screen"
    assert config.active_streamer == "app-123"
    assert config.is_streaming
    assert config.channels[0].gamut == gamut
    assert config.channels[1].gamut is None  # no member -> encoder default
