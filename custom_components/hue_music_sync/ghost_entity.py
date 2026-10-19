"""Shared base for the Movie mode (hue-ghost) entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .ghost.coordinator import HueGhostCoordinator


class HueGhostEntity(CoordinatorEntity[HueGhostCoordinator]):
    """Base for entities on the per-entry "Hue Ghost" device.

    The device carries the name, so every entity here is named for what it does
    and nothing else - "Sync status", "Global sync", "Apple TV". Home Assistant
    prefixes the device name itself, which is what keeps the entity ids short
    (``sensor.hue_ghost_sync_status``) and the dashboard tiles readable.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: HueGhostCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.manager.entry.entry_id
        self._attr_unique_id = f"{entry_id}_ghost_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_ghost")},
            name="Hue Ghost",
            manufacturer="hue-ghost",
            model="Software Hue Sync Box (Jellyfin)",
            configuration_url=coordinator.client.base_url + "/status",
        )

    # One plain sentence on what this entity does and what its values mean,
    # shown under Attributes in the entity's details (Home Assistant has no
    # description field of its own for entities).
    _description: str = ""

    @property
    def available(self) -> bool:
        # Stay available while offline so the switch can still be used to
        # (re)enable once the PC is back; the state sensor reports "offline".
        return True

    @property
    def extra_state_attributes(self) -> dict | None:
        attrs = dict(self._ghost_attributes() or {})
        if self._description:
            attrs["description"] = self._description
        return attrs or None

    def _ghost_attributes(self) -> dict | None:
        """What a subclass reports besides its description."""
        return None


class HueGhostRemoteEntity(HueGhostEntity):
    """A control that needs Hue Ghost 2.13 or newer on the PC. Unavailable
    while the PC runs an older version (it has nothing to set); kept available
    while the PC is offline, like the rest of the device."""

    @property
    def available(self) -> bool:
        return not self.coordinator.online or self.coordinator.remote_control
