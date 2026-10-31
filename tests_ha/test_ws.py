"""The card's WebSocket API: reading the library backend is open to dashboard
users, switching it writes the entry's options and takes an admin."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hue_music_sync import DATA_AREA_INDEX
from custom_components.hue_music_sync.const import (
    BACKEND_MA,
    BACKEND_SUBSONIC,
    CONF_ACTIVE_BACKEND,
    DOMAIN,
)
from custom_components.hue_music_sync.ws import async_register_ws

SWITCH = "switch.living_room_sync"


async def _setup(hass) -> MockConfigEntry:
    assert await async_setup_component(hass, "websocket_api", {})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[DATA_AREA_INDEX] = {
        SWITCH: (SimpleNamespace(entry=entry), "area-1")
    }
    async_register_ws(hass)
    return entry


async def test_admin_switches_the_backend(hass, hass_ws_client):
    entry = await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": f"{DOMAIN}/backend", "entity_id": SWITCH, "set": BACKEND_SUBSONIC}
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    assert entry.options[CONF_ACTIVE_BACKEND] == BACKEND_SUBSONIC


async def test_non_admin_reads_but_cannot_switch(
    hass, hass_ws_client, hass_read_only_access_token
):
    entry = await _setup(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/backend", "entity_id": SWITCH})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["available"][BACKEND_MA] is True

    await client.send_json(
        {"id": 2, "type": f"{DOMAIN}/backend", "entity_id": SWITCH, "set": BACKEND_SUBSONIC}
    )
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "unauthorized"
    assert CONF_ACTIVE_BACKEND not in entry.options
