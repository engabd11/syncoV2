"""Button entity: analyse the whole music library (track-map pre-warm)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .library_entity import HueSyncoLibraryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([HueSyncoPrewarmButton(entry)])


class HueSyncoPrewarmButton(HueSyncoLibraryEntity, ButtonEntity):
    """Kicks off (or resumes) the library analysis sweep.

    Same as calling the ``hue_music_sync.prewarm_library`` service; pressing
    while a sweep runs is a no-op (the service guards re-entry). Progress shows
    on the companion "Library analysis" sensor.
    """

    _attr_name = "Analyse library"
    _attr_icon = "mdi:music-box-multiple"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "prewarm_button")

    async def async_press(self) -> None:
        await self.hass.services.async_call(DOMAIN, "prewarm_library", {})
