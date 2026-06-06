"""Select entities: colour scheme, effect mode and followed media player."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ColorScheme, EffectMode
from .coordinator import SyncManager
from .entity import HueMusicSyncAreaEntity

_AUTO = "auto"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    for area_id in manager.enabled_areas:
        entities.append(ColorSchemeSelect(manager, area_id))
        entities.append(EffectModeSelect(manager, area_id))
        entities.append(MediaPlayerSelect(manager, area_id))
    async_add_entities(entities)


class ColorSchemeSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick the active colour scheme."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "color_scheme"
    _attr_icon = "mdi:palette"
    _attr_options = [str(s) for s in ColorScheme]

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "color_scheme")

    @property
    def current_option(self) -> str:
        return str(self._manager.get_settings(self._area_id).color_scheme)

    async def async_select_option(self, option: str) -> None:
        await self._manager.update_settings(
            self._area_id, color_scheme=ColorScheme(option)
        )
        self.async_write_ha_state()


class EffectModeSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick the choreography mode."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "effect_mode"
    _attr_icon = "mdi:auto-fix"
    _attr_options = [str(m) for m in EffectMode]

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "effect_mode")

    @property
    def current_option(self) -> str:
        return str(self._manager.get_settings(self._area_id).effect_mode)

    async def async_select_option(self, option: str) -> None:
        await self._manager.update_settings(
            self._area_id, effect_mode=EffectMode(option)
        )
        self.async_write_ha_state()


class MediaPlayerSelect(HueMusicSyncAreaEntity, SelectEntity):
    """Pick which media player to follow ('auto' = first one playing)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "media_player"
    _attr_icon = "mdi:speaker"

    def __init__(self, manager: SyncManager, area_id: str) -> None:
        super().__init__(manager, area_id, "media_player")

    @property
    def options(self) -> list[str]:
        players = sorted(
            s.entity_id for s in self.hass.states.async_all("media_player")
        )
        return [_AUTO, *players]

    @property
    def current_option(self) -> str:
        return self._manager.get_settings(self._area_id).media_player or _AUTO

    async def async_select_option(self, option: str) -> None:
        value = None if option == _AUTO else option
        await self._manager.update_settings(self._area_id, media_player=value)
        self.async_write_ha_state()
