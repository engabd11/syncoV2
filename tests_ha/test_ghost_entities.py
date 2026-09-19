"""Movie mode (hue-ghost) hand-over rules.

A Hue bridge lets exactly one application stream to an entertainment area, so
movie mode and music sync must never fight over it: switching movie mode on
stops every active music-sync area first, and starting music sync asks movie
mode to let go. These tests pin those two orders down, plus the coordinator's
offline behaviour, without a bridge or a PC.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hue_music_sync.coordinator import SyncManager
from custom_components.hue_music_sync.ghost.client import HueGhostError
from custom_components.hue_music_sync.ghost.coordinator import HueGhostCoordinator


def _manager(hass) -> SyncManager:
    """A SyncManager with only the start/stop bookkeeping the tests touch."""
    manager = SyncManager.__new__(SyncManager)
    manager.hass = hass
    entry = MockConfigEntry(domain="hue_music_sync", data={}, entry_id="entry-1")
    entry.add_to_hass(hass)
    manager.entry = entry
    manager._sessions = {}
    manager.ghost = None
    manager._stop_area_locked = AsyncMock()
    return manager


def _coordinator(hass, manager, status=None, fail=False) -> HueGhostCoordinator:
    client = AsyncMock()
    client.base_url = "http://pc:8787"
    if fail:
        client.status = AsyncMock(side_effect=HueGhostError("hue-ghost unreachable at http://pc:8787"))
    else:
        client.status = AsyncMock(return_value=status or {"enabled": True, "state": "idle"})
    return HueGhostCoordinator(hass, manager, client)


async def test_turn_on_stops_music_sync_everywhere_first(hass):
    hass.data.setdefault("hue_music_sync", {})
    manager = _manager(hass)
    manager._sessions = {"area-1": object()}
    hass.data["hue_music_sync"]["entry-1"] = manager
    order: list[str] = []
    manager._stop_area_locked = AsyncMock(side_effect=lambda area: order.append(f"stop:{area}"))
    coordinator = _coordinator(hass, manager)
    coordinator.client.set_enabled = AsyncMock(side_effect=lambda on: order.append(f"ghost:{on}"))
    manager.ghost = coordinator

    await coordinator.async_turn_on()

    assert order == ["stop:area-1", "ghost:True"]


async def test_turn_on_surfaces_unreachable_pc_as_ha_error(hass):
    hass.data.setdefault("hue_music_sync", {})
    manager = _manager(hass)
    hass.data["hue_music_sync"]["entry-1"] = manager
    coordinator = _coordinator(hass, manager)
    coordinator.client.set_enabled = AsyncMock(side_effect=HueGhostError("hue-ghost unreachable"))
    with pytest.raises(HomeAssistantError, match="unreachable"):
        await coordinator.async_turn_on()


async def test_music_start_yields_movie_mode(hass):
    hass.data.setdefault("hue_music_sync", {})
    manager = _manager(hass)
    hass.data["hue_music_sync"]["entry-1"] = manager
    coordinator = _coordinator(hass, manager, status={"enabled": True, "state": "syncing"})
    await coordinator.async_refresh()
    assert coordinator.enabled is True
    manager.ghost = coordinator

    await manager._yield_movie_mode()

    coordinator.client.set_enabled.assert_awaited_once_with(False)


async def test_yield_is_a_no_op_when_movie_mode_is_off(hass):
    manager = _manager(hass)
    coordinator = _coordinator(hass, manager, status={"enabled": False, "state": "idle"})
    await coordinator.async_refresh()
    manager.ghost = coordinator
    hass.data.setdefault("hue_music_sync", {})["entry-1"] = manager

    await manager._yield_movie_mode()

    coordinator.client.set_enabled.assert_not_awaited()


async def test_offline_pc_reports_offline_but_does_not_break_setup(hass):
    manager = _manager(hass)
    coordinator = _coordinator(hass, manager, fail=True)
    await coordinator.async_refresh()
    assert coordinator.online is False
    assert coordinator.state == "offline"
    assert coordinator.enabled is False
    assert coordinator.data is None
