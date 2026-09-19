"""Polls hue-ghost's /status and arbitrates the entertainment area between
movie mode (hue-ghost + Hue Sync app) and this integration's music sync."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import HueGhostClient, HueGhostError, summarize_status

if TYPE_CHECKING:
    from ..coordinator import SyncManager

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=5)


class HueGhostCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One per config entry that has a hue-ghost host configured."""

    def __init__(self, hass: HomeAssistant, manager: SyncManager, client: HueGhostClient) -> None:
        super().__init__(
            hass, _LOGGER, name="hue_music_sync ghost", update_interval=SCAN_INTERVAL,
            config_entry=manager.entry,
        )
        self.manager = manager
        self.client = client
        self.raw: dict[str, Any] | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            self.raw = await self.client.status()
        except HueGhostError as err:
            self.raw = None
            raise UpdateFailed(str(err)) from err
        return summarize_status(self.raw)

    # -- derived state -----------------------------------------------------------
    @property
    def online(self) -> bool:
        return self.last_update_success and self.raw is not None

    @property
    def enabled(self) -> bool:
        return bool(self.raw and self.raw.get("enabled"))

    @property
    def state(self) -> str:
        if not self.online:
            return "offline"
        return str((self.raw or {}).get("state") or "idle")

    # -- actions ----------------------------------------------------------------------
    async def async_turn_on(self) -> None:
        """Hand the entertainment area to movie mode: stop music sync everywhere
        first (one streamer per area on the bridge), then enable hue-ghost."""
        await self.manager.stop_all_areas()
        try:
            await self.client.set_enabled(True)
        except HueGhostError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_turn_off(self) -> None:
        try:
            await self.client.set_enabled(False)
        except HueGhostError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_yield_to_music(self) -> None:
        """Best effort: called when a music-sync area starts. Never raises."""
        if not self.enabled:
            return
        try:
            await self.client.set_enabled(False)
            _LOGGER.info("Movie mode switched off so music sync can take the entertainment area")
        except HueGhostError as err:
            _LOGGER.warning("Could not switch movie mode off: %s", err)
        await self.async_request_refresh()

    async def async_set_intensity(self, level: str) -> None:
        try:
            await self.client.set_intensity(level)
        except HueGhostError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()

    async def async_set_offset(self, seconds: float) -> None:
        try:
            await self.client.set_offset(seconds)
        except HueGhostError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_request_refresh()
