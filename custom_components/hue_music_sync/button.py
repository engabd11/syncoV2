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
    async_add_entities(
        [
            HueSyncoPrewarmButton(entry),
            HueSyncoReanalyseButton(entry),
            HueSyncoClearCacheButton(entry),
        ]
    )


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


class HueSyncoReanalyseButton(HueSyncoLibraryEntity, ButtonEntity):
    """Wipe the cache and re-analyse the WHOLE library from scratch.

    Unlike "Analyse library" (which skips already-cached tracks), this forces a
    full re-analysis — the way to upgrade every track map to a newer analysis
    format. Runs in the background; progress shows on the "Library analysis"
    sensor. ``prewarm_library`` with ``force: true``.
    """

    _attr_name = "Reanalyse library"
    _attr_icon = "mdi:cached"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "reanalyse_button")

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN, "prewarm_library", {"force": True}
        )


class HueSyncoClearCacheButton(HueSyncoLibraryEntity, ButtonEntity):
    """Delete the whole on-disk library cache (every analysed track map).

    Frees the disk space; tracks re-analyse on their next play (or press
    "Analyse library" to rebuild it). ``clear_library_cache`` service.
    """

    _attr_name = "Delete library cache"
    _attr_icon = "mdi:delete-sweep"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "clear_cache_button")

    async def async_press(self) -> None:
        await self.hass.services.async_call(DOMAIN, "clear_library_cache", {})
