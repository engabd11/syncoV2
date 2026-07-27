"""Resolve the active :class:`LibraryBackend` for a config entry.

Exactly one backend is active per entry (Music Assistant or a direct
Navidrome/OpenSubsonic server), chosen in the options and read fresh on every
call so a runtime switch takes effect immediately with no reload.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    BACKEND_SUBSONIC,
    CONF_ACTIVE_BACKEND,
    CONF_SUBSONIC_PASSWORD,
    CONF_SUBSONIC_URL,
    CONF_SUBSONIC_USER,
    DEFAULT_BACKEND,
)
from .base import LibraryBackend
from .ma_backend import MABackend
from .subsonic_backend import SubsonicBackend

_LOGGER = logging.getLogger(__name__)


def active_backend_id(entry: ConfigEntry) -> str:
    """The configured backend id for this entry (defaults to Music Assistant)."""
    return entry.options.get(CONF_ACTIVE_BACKEND) or DEFAULT_BACKEND


def subsonic_configured(entry: ConfigEntry) -> bool:
    """Whether a Navidrome/OpenSubsonic URL is set (so that backend is selectable)."""
    return bool((entry.options.get(CONF_SUBSONIC_URL) or "").strip())


def resolve_backend(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    followed_player: str | None = None,
) -> LibraryBackend:
    """The active backend instance for ``entry``.

    Falls back to Music Assistant if the Subsonic backend is selected but no
    library URL is configured, so a half-set option can never leave the card
    with a dead backend.
    """
    if active_backend_id(entry) == BACKEND_SUBSONIC and subsonic_configured(entry):
        return SubsonicBackend(
            async_get_clientsession(hass),
            (entry.options.get(CONF_SUBSONIC_URL) or "").strip(),
            entry.options.get(CONF_SUBSONIC_USER, ""),
            entry.options.get(CONF_SUBSONIC_PASSWORD, ""),
        )
    return MABackend(hass, browse_entity_id=followed_player)
