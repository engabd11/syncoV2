"""Sensor entity: live progress of the library analysis (track-map pre-warm)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import _read_prewarm_state, prewarm_status
from .const import SIGNAL_PREWARM
from .library_entity import HueSyncoLibraryEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([HueSyncoPrewarmProgress(entry)])


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
