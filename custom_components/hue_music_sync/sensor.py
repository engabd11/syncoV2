"""Sensor entity: live progress of the library analysis (track-map pre-warm)."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _read_prewarm_state, prewarm_status
from .const import DOMAIN, SIGNAL_PREWARM
from .coordinator import SyncManager, trackmap_cache_stats
from .ghost.client import GHOST_STATES
from .ghost_entity import HueGhostEntity
from .library_entity import HueSyncoLibraryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities: list[SensorEntity] = [
        HueSyncoPrewarmProgress(entry),
        HueSyncoCacheSizeSensor(entry),
        HueSyncoCachedSongsSensor(entry),
    ]
    manager: SyncManager | None = hass.data[DOMAIN].get(entry.entry_id)
    if manager is not None and manager.ghost is not None:
        entities.extend(
            [
                HueGhostStateSensor(manager.ghost),
                HueGhostAreaSensor(manager.ghost),
                HueGhostSourceSensor(manager.ghost),
                HueGhostNowPlayingSensor(manager.ghost),
                HueGhostDriftSensor(manager.ghost),
            ]
        )
    async_add_entities(entities)


class _HueSyncoCacheStatSensor(HueSyncoLibraryEntity, SensorEntity):
    """Base for the disk-cache stat sensors: polled + refreshed after a sweep."""

    _attr_should_poll = True  # re-read the cache dir periodically

    async def async_added_to_hass(self) -> None:
        # Refresh immediately whenever the sweep/clear changes the cache.
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PREWARM, self._refresh)
        )
        await self.async_update()

    @callback
    def _refresh(self) -> None:
        self.async_schedule_update_ha_state(force_refresh=True)

    async def _stats(self) -> tuple[int, int]:
        return await self.hass.async_add_executor_job(trackmap_cache_stats, self.hass)


class HueSyncoCacheSizeSensor(_HueSyncoCacheStatSensor):
    """Total size on disk of the analysed track-map cache."""

    _attr_name = "Library cache size"
    _attr_icon = "mdi:database"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "cache_size")

    async def async_update(self) -> None:
        _count, size = await self._stats()
        self._attr_native_value = round(size / (1024 * 1024), 2)


class HueSyncoCachedSongsSensor(_HueSyncoCacheStatSensor):
    """How many songs (track maps) are cached on disk."""

    _attr_name = "Library cached songs"
    _attr_icon = "mdi:music-note-multiple"
    _attr_native_unit_of_measurement = "songs"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "cached_songs")

    async def async_update(self) -> None:
        count, _size = await self._stats()
        self._attr_native_value = count


class HueSyncoPrewarmProgress(HueSyncoLibraryEntity, SensorEntity):
    """Percent of the library the analysis sweep has worked through.

    100% means every analysable track has a cached track map, so each song
    reacts from its first beat. Attributes carry the detail (status, counts,
    the first failure). Driven live over the pre-warm dispatcher signal;
    seeded from the persisted sweep state after a restart.
    """

    _attr_name = "Library analysis"
    _attr_icon = "mdi:progress-clock"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "prewarm_progress")

    async def async_added_to_hass(self) -> None:
        status = prewarm_status(self.hass)
        if status["status"] == "never run":
            # Seed from the persisted sweep state so a finished (or interrupted)
            # run still shows meaningfully after a restart.
            persisted = await self.hass.async_add_executor_job(
                _read_prewarm_state, self.hass
            )
            if persisted is not None and status["status"] == "never run":
                done = persisted.get("completed", False)
                status.update(
                    {
                        "status": "complete" if done else "interrupted",
                        "total": int(persisted.get("total", 0) or 0),
                        "done": int(persisted.get("total", 0) or 0) if done else 0,
                        "analysed": int(persisted.get("analysed", 0) or 0),
                        "ambient": int(persisted.get("ambient", 0) or 0),
                        "failed": int(persisted.get("failed", 0) or 0),
                        "last_run": persisted.get("finished_at"),
                    }
                )
                # The persistent index carries the labelled failure list, so
                # "which songs failed" survives a restart too.
                from .coordinator import get_track_index

                index = get_track_index(self.hass)
                await index.ensure_loaded()
                status["failed_tracks"] = index.failed_entries(25)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PREWARM, self._updated)
        )
        self._apply(status)

    @callback
    def _updated(self) -> None:
        self._apply(prewarm_status(self.hass))
        self.async_write_ha_state()

    def _apply(self, status: dict) -> None:
        total = status.get("total") or 0
        if status.get("status") == "complete":
            self._attr_native_value = 100
        elif total:
            self._attr_native_value = min(
                100, round(100 * (status.get("done") or 0) / total)
            )
        else:
            self._attr_native_value = None  # never run / not yet counting
        self._attr_extra_state_attributes = {
            "status": status.get("status"),
            "running": status.get("running", False),
            "tracks_total": total,
            "tracks_checked": status.get("done", 0),
            "newly_analysed": status.get("analysed", 0),
            # Ambient tier: decodable, full continuous show, but no reliable
            # offline beat grid (the live tracker follows the replayed onsets).
            "newly_ambient": status.get("ambient", 0),
            "failed": status.get("failed", 0),
            # Tracks enumerated but not yet analysed (new since the last run).
            "pending": status.get("pending"),
            "last_run": status.get("last_run"),
            "last_error": status.get("last_error"),
            # Capped labelled list; the full report is written next to the
            # cache as analysis_report.json after each sweep.
            "failed_tracks": status.get("failed_tracks", []),
        }


class HueGhostStateSensor(HueGhostEntity, SensorEntity):
    """Sync status: offline / idle / ghosting / syncing.

    The one sensor to put on a card next to the light. Attributes carry the
    detail - what is playing, its position, the measured drift between the
    ghost and the TV, and the Hue Sync app's own state.
    """

    _attr_translation_key = "ghost_state"
    _attr_icon = "mdi:ghost"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(GHOST_STATES)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "state")

    @property
    def native_value(self) -> str:
        state = self.coordinator.state
        return state if state in GHOST_STATES else "idle"

    @property
    def extra_state_attributes(self) -> dict | None:
        data = dict(self.coordinator.data or {})
        data.pop("state", None)
        return data or None


class HueGhostAreaSensor(HueGhostEntity, SensorEntity):
    """Which entertainment area the sync plays in.

    The area is picked in the Hue Sync app (per source, in hue-ghost), so it
    is reported rather than set: this answers "where are the lights going to
    go when I press play", which matters once more than one room can be the
    one syncing.
    """

    _attr_translation_key = "ghost_area"
    _attr_icon = "mdi:sofa"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "area")

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("area")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "area_id": data.get("area_id"),
            "source": data.get("active_source"),
            "syncing": data.get("state") == "syncing",
            # Every source can target its own area; this is where each would go.
            "areas_by_source": {
                b.get("name") or b["id"]: b.get("area_name")
                for b in self.coordinator.bindings
                if b.get("enabled")
            },
        }


class HueGhostSourceSensor(HueGhostEntity, SensorEntity):
    """Which followed source is driving the lights right now.

    Not the same question as which sources are switched on: several can be
    followed, one plays.
    """

    _attr_translation_key = "ghost_source"
    _attr_icon = "mdi:import"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "source")

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("active_source")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        live = data.get("active_source_id")
        binding = self.coordinator.binding(live) if live else None
        return {
            "source_id": live,
            "kind": (binding or {}).get("source") or data.get("source_kind"),
            "device": (binding or {}).get("device") or (binding or {}).get("exe"),
            "area": (binding or {}).get("area_name") or data.get("area"),
            "followed": [
                b.get("name") or b["id"] for b in self.coordinator.bindings if b.get("enabled")
            ],
        }


class HueGhostNowPlayingSensor(HueGhostEntity, SensorEntity):
    """What is on screen: the Jellyfin item the TV is playing, or whatever the
    PC source reported (a window title, a game)."""

    _attr_translation_key = "ghost_now_playing"
    _attr_icon = "mdi:play-box-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "now_playing")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        title = data.get("now_playing") or data.get("source_title")
        # A state is capped at 255 characters; a long title must not break it.
        return title[:255] if isinstance(title, str) else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "source": data.get("active_source"),
            "position_s": data.get("position_s"),
            "paused": data.get("paused"),
            "device": data.get("followed_device"),
        }


class HueGhostDriftSensor(HueGhostEntity, SensorEntity):
    """How far the ghost copy is from the TV, in seconds.

    Diagnostic: a steady non-zero reading while syncing is what the sync offset
    is for, so the number to watch from the couch while tuning it. Signed -
    positive means the ghost (and so the lights) runs ahead.
    """

    _attr_translation_key = "ghost_drift"
    _attr_icon = "mdi:swap-horizontal"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "drift")

    @property
    def native_value(self) -> float | None:
        value = (self.coordinator.data or {}).get("drift_s")
        return round(float(value), 3) if value is not None else None
