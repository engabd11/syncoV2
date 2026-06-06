"""Number entities: latency offset (ms) and intensity."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []
    for area_id in manager.enabled_areas:
        entities.append(LatencyNumber(manager, area_id))
        entities.append(IntensityNumber(manager, area_id))
    async_add_entities(entities)


class LatencyNumber(HueMusicSyncAreaEntity, NumberEntity):
    """Latency offset so lights line up with what the listener hears."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "latency"
    _attr_icon = "mdi:timer-sync"
    _attr_native_min_value = 0
    _attr_native_max_value = 1000
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "latency")

    @property
    def native_value(self) -> float:
        return self._manager.get_settings(self._area_id).latency_ms

    async def async_set_native_value(self, value: float) -> None:
        await self._manager.update_settings(self._area_id, latency_ms=int(value))
        self.async_write_ha_state()


class IntensityNumber(HueMusicSyncAreaEntity, NumberEntity):
    """Overall brightness scaling for the effect (1.0 = nominal)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "intensity"
    _attr_icon = "mdi:brightness-percent"
    _attr_native_min_value = 0.1
    _attr_native_max_value = 1.5
    _attr_native_step = 0.05
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "intensity")

    @property
    def native_value(self) -> float:
        return self._manager.get_settings(self._area_id).intensity

    async def async_set_native_value(self, value: float) -> None:
        await self._manager.update_settings(self._area_id, intensity=round(value, 2))
        self.async_write_ha_state()
