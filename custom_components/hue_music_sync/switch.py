"""Switch entity: activate/deactivate music sync for an entertainment area."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DATA_AREA_INDEX
from .const import DOMAIN, signal_area_update
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HueMusicSyncSwitch(manager, area_id) for area_id in manager.enabled_areas
    )


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
        await self._manager.stop_area(self._area_id)
        self._sync_state()
