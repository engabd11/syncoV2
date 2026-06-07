"""Shared base entity for per-area Hue Music Sync entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import SyncManager


class HueMusicSyncAreaEntity(Entity):
    """Base for entities that belong to one entertainment area."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: SyncManager, area_id: str, key: str) -> None:
        self._manager = manager
        self._area_id = area_id
        entry_id = manager.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{area_id}_{key}"
        config = manager.configs.get(area_id)
        area_name = config.name if config else area_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{area_id}")},
            name=f"Music Sync — {area_name}",
            manufacturer="Signify (Philips Hue)",
            model="Entertainment area",
        )
