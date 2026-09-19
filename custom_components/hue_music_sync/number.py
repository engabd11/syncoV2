"""Number entity: master brightness (separate from intensity/mode)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity
from .ghost_entity import HueGhostEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []
    for area_id in manager.enabled_areas:
        entities.append(BrightnessNumber(manager, area_id))
        entities.append(TimingNumber(manager, area_id))
    if manager.ghost is not None:
        entities.append(HueGhostOffsetNumber(manager.ghost))
    async_add_entities(entities)


class BrightnessNumber(HueMusicSyncAreaEntity, NumberEntity):
    """Master brightness ceiling. Intensity/mode varies brightness below this."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "brightness"
    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 5
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "brightness")

    @property
    def native_value(self) -> float:
        return round(self._manager.get_settings(self._area_id).brightness * 100)

    async def async_set_native_value(self, value: float) -> None:
        await self._manager.update_settings(self._area_id, brightness=value / 100.0)
        self.async_write_ha_state()


class TimingNumber(HueMusicSyncAreaEntity, NumberEntity):
    """Nudge the lights earlier/later to match the sound (ms)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "timing"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_native_min_value = -500
    _attr_native_max_value = 500
    _attr_native_step = 10
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_mode = NumberMode.SLIDER

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "timing")

    @property
    def native_value(self) -> float:
        return self._manager.get_settings(self._area_id).timing_ms

    async def async_set_native_value(self, value: float) -> None:
        await self._manager.update_settings(self._area_id, timing_ms=int(value))
        self.async_write_ha_state()


class HueGhostOffsetNumber(HueGhostEntity, NumberEntity):
    """hue-ghost's sync offset: how far the ghost runs ahead of the TV to cancel
    the capture -> bridge -> lamp latency. Tune it from the couch: +0.25 s makes
    the lights react later, -0.25 s earlier."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ghost_offset"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = -2.0
    _attr_native_max_value = 5.0
    _attr_native_step = 0.05
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "offset")

    @property
    def native_value(self) -> float | None:
        raw = self.coordinator.raw or {}
        value = raw.get("offset_s")
        return round(float(value), 2) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_offset(value)
