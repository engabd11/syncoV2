"""Switch entity: activate/deactivate music sync for an entertainment area."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DATA_AREA_INDEX
from .const import DOMAIN, signal_area_update
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity
from .ghost_entity import HueGhostEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    for area_id in manager.enabled_areas:
        entities.append(HueMusicSyncSwitch(manager, area_id))
        entities.append(HueMusicSyncAdvancedSwitch(manager, area_id))
    if manager.ghost is not None:
        entities.append(HueGhostMovieModeSwitch(manager.ghost))
        entities.append(HueGhostAudioEffectsSwitch(manager.ghost))
        entities.extend(_binding_switches(manager.ghost, set()))
    async_add_entities(entities)

    if manager.ghost is not None:
        _watch_for_new_bindings(manager.ghost, async_add_entities)


def _binding_switches(coordinator, known: set[str]) -> list[SwitchEntity]:
    return [HueGhostBindingSwitch(coordinator, b)
            for b in coordinator.bindings if b["id"] not in known]


def _watch_for_new_bindings(coordinator, async_add_entities: AddEntitiesCallback) -> None:
    """Sources are edited in the Hue Ghost app, not here, so one added over
    there should appear here without a reload. Ones that disappear go
    unavailable rather than being deleted."""
    known = {b["id"] for b in coordinator.bindings}

    @callback
    def _check() -> None:
        fresh = [b["id"] for b in coordinator.bindings]
        new = [b for b in coordinator.bindings if b["id"] not in known]
        if new:
            known.update(fresh)
            async_add_entities([HueGhostBindingSwitch(coordinator, b) for b in new])

    coordinator.async_add_listener(_check)


class HueMusicSyncSwitch(HueMusicSyncAreaEntity, SwitchEntity):
    """Turns music sync on/off for one area."""

    _attr_name = None  # use the device name
    _attr_icon = "mdi:music-note"

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "sync")
        self._attr_is_on = manager.is_active(area_id)

    @property
    def extra_state_attributes(self) -> dict | None:
        """Now-playing, extracted album colours and detected tempo while syncing.

        Lets dashboard cards recolour to the album and lock a visualizer to the
        song. Empty (so the attributes disappear) when the area isn't active.
        """
        return self._manager.area_attributes(self._area_id) or None

    @callback
    def _sync_state(self) -> None:
        self._attr_is_on = self._manager.is_active(self._area_id)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._attr_is_on = self._manager.is_active(self._area_id)
        # Register this switch so the activate/deactivate services can target it.
        self.hass.data[DOMAIN][DATA_AREA_INDEX][self.entity_id] = (
            self._manager,
            self._area_id,
        )
        # Reconcile when session state changes outside our own turn_on/off
        # (e.g. another area takes over the bridge, or the DTLS channel dropped).
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_area_update(self._area_id), self._sync_state
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        self.hass.data[DOMAIN][DATA_AREA_INDEX].pop(self.entity_id, None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Optimistic: reflect "on" immediately during the multi-second handshake.
        self._attr_is_on = True
        self.async_write_ha_state()
        try:
            await self._manager.start_area(self._area_id)
        except Exception as err:  # noqa: BLE001
            self._attr_is_on = False
            self.async_write_ha_state()
            _LOGGER.error("Failed to start music sync for %s: %s", self._area_id, err)
            raise
        self._sync_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        try:
            await self._manager.stop_area(self._area_id)
        finally:
            self._sync_state()


class HueMusicSyncAdvancedSwitch(HueMusicSyncAreaEntity, SwitchEntity):
    """Advanced controls: reveal + apply this area's live tunable knobs.

    A per-area setting (not a live-sync action), so it lives in the device's
    Config section. When on, the dashboard card shows the tunable sliders under
    the intensity picker and their overrides are applied live; when off, the mode
    renders with its coded defaults regardless of any stored knob values.
    """

    _attr_name = "Advanced controls"
    _attr_icon = "mdi:tune-vertical"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "advanced")
        self._attr_is_on = manager.get_settings(area_id).advanced

    @callback
    def _sync_state(self) -> None:
        self._attr_is_on = self._manager.get_settings(self._area_id).advanced
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._attr_is_on = self._manager.get_settings(self._area_id).advanced
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_area_update(self._area_id), self._sync_state
            )
        )

    async def _set(self, advanced: bool) -> None:
        await self._manager.update_settings(self._area_id, advanced=advanced)
        self._sync_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class HueGhostMovieModeSwitch(HueGhostEntity, SwitchEntity):
    """Movie mode: the hue-ghost master switch.

    Turning it on stops every active music-sync area first (a bridge allows
    one streamer per entertainment area) and then enables hue-ghost, which
    starts the Hue Sync app the moment the TV plays.
    """

    _attr_name = None  # use the device name
    _attr_icon = "mdi:movie-open-play"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "movie_mode")

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    @property
    def extra_state_attributes(self) -> dict | None:
        return self.coordinator.data or None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_off()
class HueGhostAudioEffectsSwitch(HueGhostEntity, SwitchEntity):
    """Hue Sync's "use audio for light effects" for video and games mode: the
    lights react to the soundtrack as well as the picture.

    hue-ghost leaves this to the Hue Sync app until you touch it - so until
    then the switch reports what the app itself is set to. Flipping it takes
    ownership, and hue-ghost applies it at the start of the next sync (the app
    only reads this setting when it launches, so it is restarted for it).
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ghost_audio_effects"
    _attr_icon = "mdi:music-circle-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "audio_effects")

    @property
    def is_on(self) -> bool:
        raw = self.coordinator.raw or {}
        wanted = raw.get("use_audio")
        if wanted is None:                                  # not ours: mirror the app
            return bool((raw.get("engine") or {}).get("use_audio"))
        return bool(wanted)

    @property
    def extra_state_attributes(self) -> dict:
        raw = self.coordinator.raw or {}
        return {
            "set_by": "hue_ghost" if raw.get("use_audio") is not None else "hue_sync_app",
            "mode": raw.get("mode"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_use_audio(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_use_audio(False)


class HueGhostBindingSwitch(HueGhostEntity, SwitchEntity):
    """One source the PC can follow: a Jellyfin client, or an app on that PC.

    Off means "ignore it" - it stays configured. The `active` attribute is the
    different question of whether this is the one driving the lights *now*."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ghost_binding"

    def __init__(self, coordinator, binding: dict) -> None:
        super().__init__(coordinator, f"binding_{binding['id']}")
        self._key = binding["id"]
        self._attr_translation_placeholders = {"name": binding.get("name") or binding["id"]}
        self._attr_icon = ("mdi:monitor-dashboard" if binding.get("source") == "pc"
                           else "mdi:television-play")

    @property
    def _binding(self) -> dict:
        return self.coordinator.binding(self._key) or {}

    @property
    def available(self) -> bool:
        # the base class stays available while the PC is off; a source that was
        # deleted over there is a different matter
        return bool(self._binding) or not self.coordinator.online

    @property
    def is_on(self) -> bool:
        return bool(self._binding.get("enabled"))

    @property
    def extra_state_attributes(self) -> dict:
        b = self._binding
        data = self.coordinator.data or {}
        return {
            "active": bool(b.get("active")),
            "source": b.get("source"),
            "area": b.get("area_name"),
            "device": b.get("device") or b.get("exe"),
            "mode": b.get("mode"),
            "problems": b.get("problems") or [],
            "now_playing": data.get("now_playing") if b.get("active") else None,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_binding(self._key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_binding(self._key, False)
