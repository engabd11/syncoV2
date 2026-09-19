"""Shared base for the Movie mode (hue-ghost) entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .ghost.coordinator import HueGhostCoordinator


class HueGhostEntity(CoordinatorEntity[HueGhostCoordinator]):
    """Base for entities on the per-entry "Hue Ghost — Movie mode" device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HueGhostCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.manager.entry.entry_id
        self._attr_unique_id = f"{entry_id}_ghost_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_ghost")},
            name="Hue Ghost — Movie mode",
            manufacturer="hue-ghost",
            model="Software Hue Sync Box (Jellyfin)",
            configuration_url=coordinator.client.base_url + "/status",
        )

    @property
    def available(self) -> bool:
        # Stay available while offline so the switch can still be used to
        # (re)enable once the PC is back; the state sensor reports "offline".
        return True
