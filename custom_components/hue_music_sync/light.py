"""Movie mode as a light: "Global sync".

The entertainment area is what the Hue Sync app is driving, so this entity is
deliberately a light and not another number: on/off is movie mode, and the
brightness slider is the level Hue Sync runs the area at - usable in scenes,
in voice assistants and on a normal light card. It is the *only* master
control: there is no separate movie-mode switch, because a light already does
both halves of the job and two entities for one thing is one too many.

Hue Sync's Public Control protocol has no absolute brightness command, only a
signed step; hue-ghost turns a level into that step against the level the app
reports, so a plain 0-255 slider works here.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SyncManager
from .ghost_entity import HueGhostEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    manager: SyncManager = hass.data[DOMAIN][entry.entry_id]
    if manager.ghost is not None:
        async_add_entities([HueGhostLight(manager.ghost)])


def _to_ha(level: int | None) -> int | None:
    """Hue Sync reports 0-100; Home Assistant wants 0-255."""
    if level is None:
        return None
    return max(0, min(255, round(int(level) * 255 / 100)))


def _to_ghost(brightness: int) -> int:
    return max(0, min(100, round(int(brightness) * 100 / 255)))


class HueGhostLight(HueGhostEntity, LightEntity):
    """The entertainment area movie mode drives - the master on/off + level."""

    _attr_translation_key = "ghost_light"
    _attr_icon = "mdi:television-ambient-light"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "light")

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    @property
    def brightness(self) -> int | None:
        return _to_ha((self.coordinator.data or {}).get("brightness"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "area": data.get("area"),
            "mode": data.get("mode"),
            "intensity": data.get("intensity"),
            "source": data.get("active_source"),
            "now_playing": data.get("now_playing") or data.get("source_title"),
            "syncing": data.get("state") == "syncing",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        # turning it on is movie mode: hand the area over from music sync first
        if not self.coordinator.enabled:
            await self.coordinator.async_turn_on()
        if ATTR_BRIGHTNESS in kwargs:
            await self.coordinator.async_set_brightness(_to_ghost(kwargs[ATTR_BRIGHTNESS]))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_turn_off()
