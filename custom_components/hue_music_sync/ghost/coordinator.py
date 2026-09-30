"""Polls hue-ghost's /status and arbitrates the entertainment area between
movie mode (hue-ghost + Hue Sync app) and this integration's music sync."""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import HueGhostClient, HueGhostError, bindings_of, ghost_state, summarize_status

if TYPE_CHECKING:
    from ..coordinator import SyncManager

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=3)

# Polls in a row that may fail before the PC counts as offline. One slow
# answer (the daemon busy launching mpv or restarting Hue Sync) used to flip
# Global sync to "off" and back - an on/off edge automations fired on.
OFFLINE_AFTER_FAILURES = 3


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
        self._failures = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            raw = await self.client.status()
        except HueGhostError as err:
            self._failures += 1
            if self.raw is not None and self._failures < OFFLINE_AFTER_FAILURES:
                # keep the last answer: a blip is not the PC going away
                _LOGGER.debug("hue-ghost poll failed (%d in a row), keeping last state: %s",
                              self._failures, err)
                return summarize_status(self.raw)
            self.raw = None
            raise UpdateFailed(str(err)) from err
        self._failures = 0
        self.raw = raw
        return summarize_status(raw)

    @property
    def bindings(self) -> list[dict[str, Any]]:
        """Every source the PC can follow, in its own priority order."""
        return bindings_of(self.raw)

    def binding(self, key: str) -> dict[str, Any] | None:
        return next((b for b in self.bindings if b.get("id") == key), None)

    # -- derived state -----------------------------------------------------------
    @property
    def online(self) -> bool:
        return self.last_update_success and self.raw is not None

    @property
    def enabled(self) -> bool:
        return bool(self.online and self.raw and self.raw.get("enabled"))

    @property
    def state(self) -> str:
        return ghost_state(self.raw) if self.online else "offline"

    # -- actions ----------------------------------------------------------------------
    async def _call(self, request: Awaitable[dict[str, Any]]) -> None:
        """Run one command, show its outcome at once, then read the full state.

        /on, /off and /set answer with the daemon's enabled flag and state as
        they are AFTER the command, so those land on the entities right away
        instead of on the next poll - an automation that switches Global sync
        and then checks it sees the new value."""
        try:
            resp = await request
        except HueGhostError as err:
            raise HomeAssistantError(str(err)) from err
        if self.raw is not None and isinstance(resp, dict):
            fresh = {k: resp[k] for k in ("enabled", "state") if k in resp}
            if fresh:
                self.raw = {**self.raw, **fresh}
                self._failures = 0
                self.async_set_updated_data(summarize_status(self.raw))
        await self.async_request_refresh()

    async def async_turn_on(self) -> None:
        """Hand the entertainment area to movie mode: stop music sync everywhere
        first (one streamer per area on the bridge), then enable hue-ghost."""
        await self.manager.stop_all_areas()
        await self._call(self.client.set_enabled(True))

    async def async_turn_off(self) -> None:
        await self._call(self.client.set_enabled(False))

    async def async_yield_to_music(self) -> None:
        """Best effort: called when a music-sync area starts. Never raises."""
        if not self.enabled:
            return
        _LOGGER.info("Switching Hue Ghost's Global sync off so music sync can take the "
                     "entertainment area (switch it back on to hand the area back)")
        try:
            await self._call(self.client.set_enabled(False))
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch movie mode off: %s", err)

    async def async_set_intensity(self, level: str) -> None:
        await self._call(self.client.set_intensity(level))

    async def async_set_mode(self, mode: str) -> None:
        await self._call(self.client.set_mode(mode))

    async def async_set_use_audio(self, on: bool | None) -> None:
        await self._call(self.client.set_use_audio(on))

    async def async_set_brightness(self, level: int) -> None:
        await self._call(self.client.set_brightness(level))

    async def async_set_binding(self, key: str, on: bool) -> None:
        await self._call(self.client.set_binding(key, on))

    async def async_set_offset(self, seconds: float) -> None:
        await self._call(self.client.set_offset(seconds))
