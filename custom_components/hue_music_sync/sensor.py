"""Sensor entity: live progress of the library analysis (track-map pre-warm)."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _read_prewarm_state, prewarm_status
from .const import SIGNAL_PREWARM
from .coordinator import trackmap_cache_stats
from .library_entity import HueSyncoLibraryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            HueSyncoPrewarmProgress(entry),
            HueSyncoCacheSizeSensor(entry),
            HueSyncoCachedSongsSensor(entry),
        ]
    )


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
