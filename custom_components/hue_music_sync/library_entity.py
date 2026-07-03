"""Shared base for the library-analysis entities (button + progress sensor)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class HueSyncoLibraryEntity(Entity):
    """Base for entities on the per-entry "Library" device.

    The library analysis is global to the integration (one disk cache), but
    entities need a config entry to live on; one Library device per entry keeps
    them discoverable next to the area devices.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_library")},
            name="Hue Synco Library",
            manufacturer="Hue Synco",
            model="Track-map analysis",
        )
