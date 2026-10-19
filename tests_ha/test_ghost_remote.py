"""Hue Ghost remote controls (Hue Ghost 2.13+): the source to sync now, each
source's area, mode and intensity, every timing, the time lock, keeping the
displays awake and a restart - and the description every Hue Ghost entity
carries."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hue_music_sync.coordinator import SyncManager
from custom_components.hue_music_sync.ghost.coordinator import HueGhostCoordinator

BINDINGS = [
    {"id": "apple-tv", "name": "Apple TV", "source": "jellyfin", "enabled": True, "active": True,
     "area_id": "a1", "area_name": "Living room", "mode": None, "intensity": "subtle", "problems": []},
    {"id": "camusic", "name": "Camusic linux", "source": "jellyfin", "enabled": True, "active": False,
     "area_id": None, "area_name": None, "mode": "music", "intensity": None, "problems": []},
]
SETTINGS = {
    "offset_s": -0.1, "music_offset_s": None, "lights_off_delay_s": 1.5, "idle_stop_delay_s": 10.0,
    "music_gap_s": 10.0, "music_pause_stop_s": 15.0, "pause_stop_min": 0.0, "time_lock": True,
    "keep_awake": "playing", "pixel_shift_min": 0.0, "blackout_idle_min": 1.0, "manage_area": True,
}
STATUS = {
    "enabled": True, "state": "syncing", "mode": "video", "intensity": "subtle", "offset_s": -0.1,
    "engine": {"name": "huesync", "area_name": "Living room", "syncing": True},
    "follow": {}, "bindings": BINDINGS, "pin": None, "settings": SETTINGS,
    "areas": [{"id": "a1", "name": "Living room"}, {"id": "a2", "name": "Office full music"}],
    "time_lock": {"enabled": True, "engaged": True},
}


def _manager(hass) -> SyncManager:
    manager = SyncManager.__new__(SyncManager)
    manager.hass = hass
    entry = MockConfigEntry(domain="hue_music_sync", data={}, entry_id="entry-1")
    entry.add_to_hass(hass)
    manager.entry = entry
    manager._sessions = {}
    manager.ghost = None
    manager._stop_area_locked = AsyncMock()
    return manager


async def _ready(hass, status=None) -> HueGhostCoordinator:
    manager = _manager(hass)
    client = AsyncMock()
    client.base_url = "http://pc:8787"
    client.status = AsyncMock(return_value=status or STATUS)
    coordinator = HueGhostCoordinator(hass, manager, client)
    await coordinator.async_refresh()
    manager.ghost = coordinator
    return coordinator


# -- the source to sync now ---------------------------------------------------------------

async def test_sync_source_lists_automatic_then_every_source(hass):
    from custom_components.hue_music_sync.ghost_remote import HueGhostSyncSourceSelect

    sel = HueGhostSyncSourceSelect(await _ready(hass))
    assert sel.options == ["automatic", "Apple TV", "Camusic linux"]
    assert sel.current_option == "automatic"
    assert "description" in sel.extra_state_attributes


async def test_picking_a_source_pins_it_and_automatic_unpins(hass):
    from custom_components.hue_music_sync.ghost_remote import HueGhostSyncSourceSelect

    coordinator = await _ready(hass)
    coordinator.client.set_pin = AsyncMock(return_value={})
    sel = HueGhostSyncSourceSelect(coordinator)
    await sel.async_select_option("Camusic linux")
    await sel.async_select_option("automatic")
    assert [c.args for c in coordinator.client.set_pin.await_args_list] == [("camusic",), (None,)]


async def test_the_pinned_source_reads_back(hass):
    from custom_components.hue_music_sync.ghost_remote import HueGhostSyncSourceSelect

    sel = HueGhostSyncSourceSelect(await _ready(hass, {**STATUS, "pin": "camusic"}))
    assert sel.current_option == "Camusic linux"


# -- each source's area, mode and intensity ------------------------------------------------

async def test_each_source_gets_an_area_a_mode_and_an_intensity(hass):
    from custom_components.hue_music_sync.ghost_remote import (
        HueGhostBindingAreaSelect, HueGhostBindingIntensitySelect, HueGhostBindingModeSelect,
        binding_selects)

    coordinator = await _ready(hass)
    made = binding_selects(coordinator, coordinator.bindings)
    assert len(made) == 6
    tv_area = HueGhostBindingAreaSelect(coordinator, BINDINGS[0])
    cam_area = HueGhostBindingAreaSelect(coordinator, BINDINGS[1])
    assert tv_area.options == ["Hue Sync's current area", "Living room", "Office full music"]
    assert tv_area.current_option == "Living room"
    assert cam_area.current_option == "Hue Sync's current area"
    assert HueGhostBindingModeSelect(coordinator, BINDINGS[0]).current_option == "default"
    assert HueGhostBindingModeSelect(coordinator, BINDINGS[1]).current_option == "music"
    assert HueGhostBindingIntensitySelect(coordinator, BINDINGS[0]).current_option == "subtle"
    assert tv_area.unique_id == "entry-1_ghost_binding_apple-tv_area"
    assert tv_area.translation_placeholders == {"name": "Apple TV"}


async def test_choosing_an_area_mode_or_intensity_sends_it_for_that_source(hass):
    from custom_components.hue_music_sync.ghost_remote import (
        HueGhostBindingAreaSelect, HueGhostBindingIntensitySelect, HueGhostBindingModeSelect)

    coordinator = await _ready(hass)
    coordinator.client.set_binding_fields = AsyncMock(return_value={})
    await HueGhostBindingAreaSelect(coordinator, BINDINGS[1]).async_select_option("Office full music")
    await HueGhostBindingAreaSelect(coordinator, BINDINGS[0]).async_select_option("Hue Sync's current area")
    await HueGhostBindingModeSelect(coordinator, BINDINGS[0]).async_select_option("default")
    await HueGhostBindingIntensitySelect(coordinator, BINDINGS[1]).async_select_option("extreme")
    calls = [(c.args, c.kwargs) for c in coordinator.client.set_binding_fields.await_args_list]
    assert calls == [
        (("camusic",), {"area_id": "a2"}),
        (("apple-tv",), {"area_id": ""}),
        (("apple-tv",), {"mode": ""}),
        (("camusic",), {"intensity": "extreme"}),
    ]


# -- timings and the rest ------------------------------------------------------------------

async def test_every_timing_reads_back_and_sets_by_name(hass):
    from custom_components.hue_music_sync.ghost_remote import NUMBERS, HueGhostSettingNumber

    coordinator = await _ready(hass)
    coordinator.client.set_values = AsyncMock(return_value={})
    by_key = {spec.key: HueGhostSettingNumber(coordinator, spec) for spec in NUMBERS}
    assert by_key["music_gap_s"].native_value == 10.0
    assert by_key["blackout_idle_min"].native_value == 1.0
    # music shares the film offset until it gets its own: show what it really uses
    assert by_key["music_offset_s"].native_value == -0.1
    assert by_key["music_offset_s"].extra_state_attributes["shared_with_films"] is True
    await by_key["music_offset_s"].async_set_native_value(0.1)
    coordinator.client.set_values.assert_awaited_once_with(music_offset_s=0.1)
    # every one of them says what it is for
    assert all(n.extra_state_attributes["description"] for n in by_key.values())


async def test_time_lock_keep_awake_and_restart(hass):
    from custom_components.hue_music_sync.ghost_remote import (
        HueGhostKeepAwakeSelect, HueGhostRestartButton, HueGhostTimeLockSwitch)

    coordinator = await _ready(hass)
    coordinator.client.set_values = AsyncMock(return_value={})
    coordinator.client.restart = AsyncMock(return_value={})
    lock = HueGhostTimeLockSwitch(coordinator)
    assert lock.is_on is True and lock.extra_state_attributes["engaged"] is True
    await lock.async_turn_off()
    awake = HueGhostKeepAwakeSelect(coordinator)
    assert awake.current_option == "playing"
    await awake.async_select_option("always")
    await HueGhostRestartButton(coordinator).async_press()
    assert [c.kwargs for c in coordinator.client.set_values.await_args_list] == \
        [{"time_lock": False}, {"keep_awake": "always"}]
    coordinator.client.restart.assert_awaited_once()


async def test_an_older_pc_leaves_the_remote_controls_unavailable(hass):
    from custom_components.hue_music_sync.ghost_remote import HueGhostSyncSourceSelect, HueGhostTimeLockSwitch

    old = {k: v for k, v in STATUS.items() if k not in ("settings", "areas", "pin")}
    coordinator = await _ready(hass, old)
    assert HueGhostSyncSourceSelect(coordinator).available is False
    assert HueGhostTimeLockSwitch(coordinator).is_on is None


async def test_the_existing_entities_describe_themselves_too(hass):
    from custom_components.hue_music_sync.light import HueGhostLight
    from custom_components.hue_music_sync.number import HueGhostOffsetNumber
    from custom_components.hue_music_sync.sensor import HueGhostAreaSensor, HueGhostStateSensor
    from custom_components.hue_music_sync.switch import HueGhostBindingSwitch

    coordinator = await _ready(hass)
    for ent in (HueGhostLight(coordinator), HueGhostOffsetNumber(coordinator),
                HueGhostStateSensor(coordinator), HueGhostAreaSensor(coordinator),
                HueGhostBindingSwitch(coordinator, BINDINGS[0])):
        attrs = ent.extra_state_attributes
        assert attrs and attrs["description"], type(ent).__name__
    # and what they reported before is still there
    assert HueGhostAreaSensor(coordinator).extra_state_attributes["source"] == "Apple TV"
