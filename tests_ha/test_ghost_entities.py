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


# -- the brightness light and the per-source switches -------------------------
BINDINGS = [
    {"id": "apple-tv", "name": "Apple TV", "source": "jellyfin", "enabled": True, "active": True,
     "area_name": "Living room", "device": "atv", "mode": None, "problems": []},
    {"id": "elden-ring", "name": "Elden Ring", "source": "pc", "enabled": False, "active": False,
     "area_name": "Gaming", "exe": "eldenring.exe", "mode": "games", "problems": []},
]
FULL_STATUS = {
    "enabled": True, "state": "syncing", "mode": "video", "intensity": "high",
    "engine": {"name": "huesync", "area_name": "Living room", "bri": 60},
    "follow": {"item": "Show - S01E02"},
    "bindings": BINDINGS,
}


async def _ready(hass, status=None):
    manager = _manager(hass)
    coordinator = _coordinator(hass, manager, status=status or FULL_STATUS)
    await coordinator.async_refresh()
    manager.ghost = coordinator
    return coordinator


async def test_the_light_reports_hue_syncs_level_as_0_255(hass):
    from custom_components.hue_music_sync.light import HueGhostLight

    light = HueGhostLight(await _ready(hass))
    assert light.is_on is True
    assert light.brightness == 153                    # 60 % of 255
    assert light.extra_state_attributes["area"] == "Living room"
    assert light.extra_state_attributes["syncing"] is True


async def test_turning_the_light_on_with_a_level_takes_the_area_then_sets_it(hass):
    from custom_components.hue_music_sync.light import HueGhostLight

    coordinator = await _ready(hass, {**FULL_STATUS, "enabled": False})
    coordinator.client.set_enabled = AsyncMock()
    coordinator.client.set_brightness = AsyncMock()
    hass.data.setdefault("hue_music_sync", {})["entry-1"] = coordinator.manager

    await HueGhostLight(coordinator).async_turn_on(brightness=128)

    coordinator.client.set_enabled.assert_awaited_once_with(True)
    coordinator.client.set_brightness.assert_awaited_once_with(50)    # 128/255


async def test_setting_only_the_level_does_not_re_enable_movie_mode(hass):
    from custom_components.hue_music_sync.light import HueGhostLight

    coordinator = await _ready(hass)                  # already on
    coordinator.client.set_enabled = AsyncMock()
    coordinator.client.set_brightness = AsyncMock()

    await HueGhostLight(coordinator).async_turn_on(brightness=255)

    coordinator.client.set_enabled.assert_not_awaited()
    coordinator.client.set_brightness.assert_awaited_once_with(100)


async def test_a_switch_per_source_says_whether_it_is_followed_and_whether_it_is_live(hass):
    from custom_components.hue_music_sync.switch import HueGhostBindingSwitch

    coordinator = await _ready(hass)
    tv = HueGhostBindingSwitch(coordinator, BINDINGS[0])
    game = HueGhostBindingSwitch(coordinator, BINDINGS[1])

    assert tv.is_on is True and game.is_on is False
    # followed is not the same question as currently driving the lights
    assert tv.extra_state_attributes["active"] is True
    assert game.extra_state_attributes["active"] is False
    assert tv.extra_state_attributes["now_playing"] == "Show - S01E02"
    assert game.extra_state_attributes["now_playing"] is None
    assert game.extra_state_attributes["source"] == "pc"
    assert game.extra_state_attributes["device"] == "eldenring.exe"
    assert tv.unique_id == "entry-1_ghost_binding_apple-tv"


async def test_switching_a_source_off_sends_its_key(hass):
    from custom_components.hue_music_sync.switch import HueGhostBindingSwitch

    coordinator = await _ready(hass)
    coordinator.client.set_binding = AsyncMock()

    await HueGhostBindingSwitch(coordinator, BINDINGS[0]).async_turn_off()

    coordinator.client.set_binding.assert_awaited_once_with("apple-tv", False)


async def test_a_source_deleted_on_the_pc_goes_unavailable(hass):
    from custom_components.hue_music_sync.switch import HueGhostBindingSwitch

    coordinator = await _ready(hass)
    sw = HueGhostBindingSwitch(coordinator, BINDINGS[1])
    assert sw.available is True
    coordinator.client.status = AsyncMock(return_value={**FULL_STATUS, "bindings": BINDINGS[:1]})
    await coordinator.async_refresh()
    assert sw.available is False


async def test_an_older_pc_without_bindings_just_has_no_source_switches(hass):
    coordinator = await _ready(hass, {"enabled": True, "state": "idle"})
    assert coordinator.bindings == []
    assert coordinator.binding("apple-tv") is None
