"""Select entity: the music-sync Mode preset for an area."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GHOST_INTENSITIES, GHOST_MODES, ColorScheme, SyncEffect, SyncMode
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity
from .ghost_entity import HueGhostEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    for area_id in manager.enabled_areas:
        entities.append(ModeSelect(manager, area_id))
        entities.append(EffectSelect(manager, area_id))
        entities.append(ColourSelect(manager, area_id))
    if manager.ghost is not None:
        entities.append(HueGhostModeSelect(manager.ghost))
        entities.append(HueGhostIntensitySelect(manager.ghost))
    async_add_entities(entities)


class ModeSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick the intensity/rhythm mode (Subtle..Intense). Does not affect colour."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "mode"
    _attr_icon = "mdi:sine-wave"
    _attr_options = [str(m) for m in SyncMode]

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "mode")

    @property
    def current_option(self) -> str:
        return str(self._manager.get_settings(self._area_id).mode)

    async def async_select_option(self, option: str) -> None:
        await self._manager.update_settings(self._area_id, mode=SyncMode(option))
        self.async_write_ha_state()


class EffectSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick the effect/renderer style (Music choreography, Fireworks, ...)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "effect"
    _attr_icon = "mdi:auto-fix"
    _attr_options = [str(e) for e in SyncEffect]

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "effect")

    @property
    def current_option(self) -> str:
        return str(self._manager.get_settings(self._area_id).effect)

    async def async_select_option(self, option: str) -> None:
        await self._manager.update_settings(self._area_id, effect=SyncEffect(option))
        self.async_write_ha_state()


class ColourSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick the colour theme: Album colours or a preset mixed-colour palette."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "colour"
    _attr_icon = "mdi:palette"
    _attr_options = [str(c) for c in ColorScheme]

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "colour")

    @property
    def current_option(self) -> str:
        return str(self._manager.get_settings(self._area_id).colour)

    async def async_select_option(self, option: str) -> None:
        await self._manager.update_settings(self._area_id, colour=ColorScheme(option))
        self.async_write_ha_state()


class HueGhostModeSelect(HueGhostEntity, SelectEntity):
    """What Hue Sync reacts to: the picture (video), the sound (music) or fast
    movement (games). hue-ghost applies it to every sync it starts, and live
    while syncing."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ghost_mode"
    _attr_icon = "mdi:movie-filter"
    _attr_options = list(GHOST_MODES)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def current_option(self) -> str | None:
        mode = (self.coordinator.raw or {}).get("mode")
        return mode if mode in GHOST_MODES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_mode(option)


class HueGhostIntensitySelect(HueGhostEntity, SelectEntity):
    """Hue Sync's video intensity preset that hue-ghost applies when sync starts."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "ghost_intensity"
    _attr_icon = "mdi:sine-wave"
    _attr_options = list(GHOST_INTENSITIES)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "intensity")

    @property
    def current_option(self) -> str | None:
        raw = self.coordinator.raw or {}
        level = raw.get("intensity")
        return level if level in GHOST_INTENSITIES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_intensity(option)
