"""Hue Ghost remote controls (Hue Ghost 2.13+ on the PC).

Everything the PC app lets you change that is worth changing from a sofa or a
phone: which source syncs now, where each source syncs and how it looks, every
timing, the time lock, keeping the displays awake, and a restart. Each entity
reads its value back from the PC's ``/status`` (its ``settings``, ``areas``,
``pin`` and ``bindings``), so the dashboard always shows what the PC is
really doing, whoever changed it.

Every entity carries a ``description`` attribute: one plain sentence on what it
does and what its values mean, because Home Assistant has no description field
of its own for entities.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import GHOST_INTENSITIES, GHOST_MODES
from .ghost.coordinator import HueGhostCoordinator
from .ghost_entity import HueGhostRemoteEntity

AUTOMATIC = "automatic"          # Sync source: the PC's own list order
CURRENT_AREA = "Hue Sync's current area"
DEFAULT = "default"              # a source's mode / intensity: the global one
KEEP_AWAKE = ("off", "playing", "always")


# -- timings and other numbers ------------------------------------------------------------

@dataclass(frozen=True)
class GhostNumber:
    key: str                     # the /set and /status "settings" key
    translation_key: str
    unit: str
    minimum: float
    maximum: float
    step: float
    icon: str
    description: str
    # music_offset_s reads None when music shares the film offset: show the
    # offset music really uses
    fallback: str | None = None


NUMBERS: tuple[GhostNumber, ...] = (
    GhostNumber(
        "music_offset_s", "ghost_music_offset", UnitOfTime.SECONDS, -2.0, 5.0, 0.05,
        "mdi:music-clef-treble",
        "Music sync offset, in seconds: moves the lights against the music. Lights after the "
        "beat? Raise it. Lights before the beat? Lower it. Used only for music; films keep "
        "the film sync offset.",
        fallback="offset_s",
    ),
    GhostNumber(
        "lights_off_delay_s", "ghost_lights_off_delay", UnitOfTime.SECONDS, 0.0, 60.0, 0.5,
        "mdi:lightbulb-off-outline",
        "How long after a film, TV show or PC app stops before the lights stop syncing. Short, "
        "so the room is back to normal straight away; a little longer rides out a player that "
        "stops and starts again. Music uses the gap between songs instead.",
    ),
    GhostNumber(
        "idle_stop_delay_s", "ghost_idle_stop_delay", UnitOfTime.SECONDS, 0.0, 600.0, 5.0,
        "mdi:timer-off-outline",
        "Standby time: how long Hue Ghost keeps its hidden copy of a film ready after the "
        "player stops. Pressing play again within this time continues at once, without "
        "reloading. The lights are already off by then.",
    ),
    GhostNumber(
        "music_gap_s", "ghost_music_gap", UnitOfTime.SECONDS, 0.0, 120.0, 1.0,
        "mdi:music-rest-quarter",
        "Gap between songs: how long the music may stop (between tracks, or when the player "
        "skips) before the lights stop syncing. Longer keeps the lights on through slow "
        "track changes.",
    ),
    GhostNumber(
        "music_pause_stop_s", "ghost_music_pause_stop", UnitOfTime.SECONDS, 0.0, 600.0, 5.0,
        "mdi:pause-circle-outline",
        "Music pause limit: how long music may stay paused before the lights stop syncing. "
        "Pressing play again starts them again. 0 keeps them syncing however long it is "
        "paused.",
    ),
    GhostNumber(
        "pause_stop_min", "ghost_film_pause_stop", UnitOfTime.MINUTES, 0.0, 240.0, 1.0,
        "mdi:pause-octagon-outline",
        "Film pause limit: how many minutes a film or TV show may stay paused before the "
        "lights stop syncing. Pressing play again starts them again. 0 keeps them syncing "
        "however long it is paused.",
    ),
    GhostNumber(
        "pixel_shift_min", "ghost_pixel_shift", UnitOfTime.MINUTES, 0.0, 60.0, 1.0,
        "mdi:monitor-shimmer",
        "Screen care for OLED displays kept awake: move the hidden film picture a few pixels "
        "every this many minutes. 0 is off.",
    ),
    GhostNumber(
        "blackout_idle_min", "ghost_blackout_idle", UnitOfTime.MINUTES, 0.0, 240.0, 1.0,
        "mdi:monitor-off",
        "Screen care: after this many minutes with no mouse or keyboard use, cover the PC's "
        "screens with black while they are kept awake. 0 is off.",
    ),
)


class HueGhostSettingNumber(HueGhostRemoteEntity, NumberEntity):
    """One of the PC's timings, set and read back over ``/set``."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: HueGhostCoordinator, spec: GhostNumber) -> None:
        super().__init__(coordinator, spec.translation_key.removeprefix("ghost_"))
        self._spec = spec
        self._attr_translation_key = spec.translation_key
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_native_min_value = spec.minimum
        self._attr_native_max_value = spec.maximum
        self._attr_native_step = spec.step
        self._attr_icon = spec.icon
        self._description = spec.description

    @property
    def native_value(self) -> float | None:
        settings = self.coordinator.settings
        value = settings.get(self._spec.key)
        if value is None and self._spec.fallback:
            value = settings.get(self._spec.fallback)
        return round(float(value), 2) if isinstance(value, (int, float)) else None

    def _ghost_attributes(self) -> dict | None:
        if self._spec.fallback and self.coordinator.settings.get(self._spec.key) is None:
            return {"shared_with_films": True}
        return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_values(**{self._spec.key: value})


# -- which source syncs, and where ---------------------------------------------------------

def _source_label(binding: dict, all_bindings: list[dict]) -> str:
    """A source's name as an option. Names are what people read, but two
    sources may share one, so a duplicate carries its id."""
    name = binding.get("name") or binding["id"]
    same = [b for b in all_bindings if (b.get("name") or b["id"]) == name]
    return name if len(same) < 2 else f"{name} ({binding['id']})"


class HueGhostSyncSourceSelect(HueGhostRemoteEntity, SelectEntity):
    """The source to sync now. Automatic keeps the PC's own list order; a
    source picked here goes first whenever it plays, and the others still sync
    while it does not. Picking a source that is switched off switches it on."""

    _attr_translation_key = "ghost_sync_source"
    _attr_icon = "mdi:cast-connected"
    _description = (
        "The source to sync now. Automatic: the first source that plays wins, in the order set "
        "in the Hue Ghost app. Pick a source to put it first whenever it plays; the others "
        "still sync while it does not. Picking a source that is switched off switches it on."
    )

    def __init__(self, coordinator: HueGhostCoordinator) -> None:
        super().__init__(coordinator, "sync_source")

    def _labels(self) -> dict[str, str]:
        binds = self.coordinator.bindings
        return {_source_label(b, binds): b["id"] for b in binds}

    @property
    def options(self) -> list[str]:
        return [AUTOMATIC, *self._labels()]

    @property
    def current_option(self) -> str | None:
        pin = self.coordinator.pin
        if not pin:
            return AUTOMATIC
        return next((label for label, bid in self._labels().items() if bid == pin), AUTOMATIC)

    def _ghost_attributes(self) -> dict | None:
        return {"source_id": self.coordinator.pin}

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_pin(None if option == AUTOMATIC else self._labels().get(option))


class _BindingEntity(HueGhostRemoteEntity):
    """A control for one source, named "<source> <what>"."""

    _what = ""

    def __init__(self, coordinator: HueGhostCoordinator, binding: dict) -> None:
        super().__init__(coordinator, f"binding_{binding['id']}_{self._what}")
        self._key = binding["id"]
        self._attr_translation_placeholders = {"name": binding.get("name") or binding["id"]}

    @property
    def _binding(self) -> dict:
        return self.coordinator.binding(self._key) or {}

    @property
    def available(self) -> bool:
        return super().available and (bool(self._binding) or not self.coordinator.online)

    def _ghost_attributes(self) -> dict | None:
        # which source this control belongs to: what the Hue Ghost card matches on
        return {"source_id": self._key, "active": bool(self._binding.get("active"))}


class HueGhostBindingAreaSelect(_BindingEntity, SelectEntity):
    _what = "area"
    _attr_translation_key = "ghost_binding_area"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sofa-outline"
    _description = (
        "The entertainment area this source syncs in. Hue Sync's current area: whatever area "
        "the Hue Sync app is set to. Changing it while this source plays moves the lights "
        "there (Hue Sync restarts for a few seconds)."
    )

    @property
    def options(self) -> list[str]:
        names = [a.get("name") or a["id"] for a in self.coordinator.areas]
        mine = self._binding.get("area_name")
        if mine and mine not in names:          # an area the PC no longer lists
            names.append(mine)
        return [CURRENT_AREA, *names]

    @property
    def current_option(self) -> str | None:
        return self._binding.get("area_name") or CURRENT_AREA

    async def async_select_option(self, option: str) -> None:
        if option == CURRENT_AREA:
            area_id = ""
        else:
            area_id = next((a["id"] for a in self.coordinator.areas
                            if (a.get("name") or a["id"]) == option), option)
        await self.coordinator.async_set_binding_fields(self._key, area_id=area_id)


class HueGhostBindingModeSelect(_BindingEntity, SelectEntity):
    _what = "mode"
    _attr_translation_key = "ghost_binding_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:movie-filter-outline"
    _attr_options = [DEFAULT, *GHOST_MODES]
    _description = (
        "What Hue Sync reacts to while this source plays: Video (the picture), Music (the "
        "sound) or Games (a fast picture). Default: Sync mode."
    )

    @property
    def current_option(self) -> str | None:
        mode = self._binding.get("mode")
        return mode if mode in GHOST_MODES else DEFAULT

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_binding_fields(self._key, mode="" if option == DEFAULT else option)


class HueGhostBindingIntensitySelect(_BindingEntity, SelectEntity):
    _what = "intensity"
    _attr_translation_key = "ghost_binding_intensity"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:sine-wave"
    _attr_options = [DEFAULT, *GHOST_INTENSITIES]
    _description = (
        "How strongly the lights react while this source plays: Subtle, Moderate, High or "
        "Extreme. Default: Sync intensity."
    )

    @property
    def current_option(self) -> str | None:
        level = self._binding.get("intensity")
        return level if level in GHOST_INTENSITIES else DEFAULT

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_binding_fields(
            self._key, intensity="" if option == DEFAULT else option)


BINDING_SELECTS = (HueGhostBindingAreaSelect, HueGhostBindingModeSelect, HueGhostBindingIntensitySelect)


# -- switches, selects and a button for the rest --------------------------------------------

class HueGhostKeepAwakeSelect(HueGhostRemoteEntity, SelectEntity):
    _attr_translation_key = "ghost_keep_awake"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:monitor-eye"
    _attr_options = list(KEEP_AWAKE)
    _description = (
        "Keep the PC's displays awake. Windows switching the displays off stops Hue Sync "
        "seeing the film. Off: never. While playing: only while a film syncs (music lets the "
        "screens sleep and keeps the PC awake). Always: all the time."
    )

    def __init__(self, coordinator: HueGhostCoordinator) -> None:
        super().__init__(coordinator, "keep_awake")

    @property
    def current_option(self) -> str | None:
        mode = self.coordinator.settings.get("keep_awake")
        return mode if mode in KEEP_AWAKE else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_values(keep_awake=option)


class HueGhostTimeLockSwitch(HueGhostRemoteEntity, SwitchEntity):
    _attr_translation_key = "ghost_time_lock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:lock-clock"
    _description = (
        "Time lock: once the lights are in step with the player, hold that timing and stop "
        "correcting, so they play together instead of chasing. Seeks, pauses and new songs or "
        "episodes still re-align them. On is recommended."
    )

    def __init__(self, coordinator: HueGhostCoordinator) -> None:
        super().__init__(coordinator, "time_lock")

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.settings.get("time_lock")
        return bool(value) if value is not None else None

    def _ghost_attributes(self) -> dict | None:
        return {"engaged": bool(((self.coordinator.raw or {}).get("time_lock") or {}).get("engaged"))}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_values(time_lock=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_values(time_lock=False)


class HueGhostRestartButton(HueGhostRemoteEntity, ButtonEntity):
    _attr_translation_key = "ghost_restart"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"
    _description = (
        "Restart the Hue Ghost app on the PC. It is back within about ten seconds and picks up "
        "where it was; use it if the PC's app stops responding."
    )

    def __init__(self, coordinator: HueGhostCoordinator) -> None:
        super().__init__(coordinator, "restart")

    async def async_press(self) -> None:
        await self.coordinator.async_restart()


# -- registration ----------------------------------------------------------------------------

def binding_selects(coordinator: HueGhostCoordinator, bindings: list[dict]) -> list[SelectEntity]:
    return [cls(coordinator, b) for b in bindings for cls in BINDING_SELECTS]


def watch_new_bindings(coordinator: HueGhostCoordinator, async_add_entities: AddEntitiesCallback,
                       make: Callable[[list[dict]], list]) -> None:
    """Sources are edited in the Hue Ghost app, so one added there gets its
    controls here without a reload."""
    known = {b["id"] for b in coordinator.bindings}

    @callback
    def _check() -> None:
        new = [b for b in coordinator.bindings if b["id"] not in known]
        if new:
            known.update(b["id"] for b in new)
            async_add_entities(make(new))

    coordinator.async_add_listener(_check)
