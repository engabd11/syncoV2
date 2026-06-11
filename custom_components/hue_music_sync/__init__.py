"""The Hue Music Sync integration."""

from __future__ import annotations

import logging
import ssl

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_APP_KEY,
    CONF_BRIGHTNESS,
    CONF_CLIENT_KEY,
    CONF_COLOUR,
    CONF_EFFECT,
    CONF_HOST,
    CONF_MEDIA_PLAYER,
    CONF_MODE,
    DOMAIN,
    PLATFORMS,
    ColorScheme,
    SyncEffect,
    SyncMode,
)
from .coordinator import SyncManager
from .hue.bridge import HueBridge, HueBridgeError

_LOGGER = logging.getLogger(__name__)

SERVICE_ACTIVATE = "activate"
SERVICE_DEACTIVATE = "deactivate"
SERVICE_SET_OPTIONS = "set_options"

# switch entity_id -> (SyncManager, area_id), populated by switch entities.
DATA_AREA_INDEX = "area_index"
DATA_CARD_REGISTERED = "card_registered"

# The bundled dashboard card (the frontend "beauty") served straight from the
# integration, so a single install gives both the backend sync and the card.
CARD_BASE_URL = "/hue_music_sync"  # the served frontend/ directory
CARD_URL = f"{CARD_BASE_URL}/hue-music-sync-card.js"


def _build_ssl_context() -> ssl.SSLContext:
    """Hue bridges use a self-signed cert on the local network."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001
        return "ffmpeg"


async def _register_frontend_card(hass: HomeAssistant) -> None:
    """Serve and register the bundled dashboard card once.

    Installing the integration then provides the Hue Synco Card automatically —
    no manual dashboard-resource step. Best-effort: a failure here never blocks
    the integration (the card can still be added as a resource by hand).
    """
    from pathlib import Path

    if hass.data[DOMAIN].get(DATA_CARD_REGISTERED):
        return
    frontend_dir = Path(__file__).parent / "frontend"
    if not (frontend_dir / "hue-music-sync-card.js").is_file():
        _LOGGER.error(
            "Hue Synco card file is missing under %s; reinstall/update the "
            "integration so the bundled card ships with it", frontend_dir
        )
        return
    hass.data[DOMAIN][DATA_CARD_REGISTERED] = True
    try:
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.loader import async_get_integration

        # Serve the whole frontend/ directory (directory static serving is the
        # most robust form), with a legacy fallback for older HA cores.
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_BASE_URL, str(frontend_dir), False)]
            )
        except (ImportError, AttributeError):
            hass.http.register_static_path(CARD_BASE_URL, str(frontend_dir), False)

        try:
            version = (await async_get_integration(hass, DOMAIN)).version
        except Exception:  # noqa: BLE001
            version = None
        url = f"{CARD_URL}?v={version}" if version else CARD_URL
        add_extra_js_url(hass, url)
        _LOGGER.info(
            "Hue Synco dashboard card registered at %s. If it doesn't appear in "
            "the card picker, hard-refresh the browser (Ctrl+Shift+R).", url
        )
    except Exception:  # noqa: BLE001 - never block setup on the card
        hass.data[DOMAIN][DATA_CARD_REGISTERED] = False
        _LOGGER.warning(
            "Could not auto-register the Hue Synco dashboard card; add it manually "
            "as a dashboard resource pointing at %s (module).", CARD_URL,
            exc_info=True,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hue Synco from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_AREA_INDEX, {})
    await _register_frontend_card(hass)

    if "music_assistant" not in hass.config.components:
        _LOGGER.warning(
            "Music Assistant integration not detected; music sync needs it to "
            "provide the audio stream"
        )

    session = async_get_clientsession(hass)
    ssl_ctx = await hass.async_add_executor_job(_build_ssl_context)
    host = entry.data[CONF_HOST]
    bridge = HueBridge(session, host, entry.data[CONF_APP_KEY], ssl_ctx)

    try:
        configs = await bridge.get_entertainment_configs()
    except (HueBridgeError, OSError) as err:
        raise ConfigEntryNotReady(f"Cannot reach Hue bridge {host}: {err}") from err

    manager = SyncManager(
        hass,
        entry,
        bridge,
        host,
        entry.data[CONF_APP_KEY],
        entry.data[CONF_CLIENT_KEY],
        _ffmpeg_binary(hass),
        configs,
    )
    hass.data[DOMAIN][entry.entry_id] = manager
    entry.runtime_data = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        manager: SyncManager = hass.data[DOMAIN].pop(entry.entry_id)
        await manager.async_shutdown()
        # Drop this manager's switch entries from the area index.
        index = hass.data[DOMAIN].get(DATA_AREA_INDEX, {})
        for eid in [k for k, (m, _) in index.items() if m is manager]:
            index.pop(eid, None)
    return unloaded


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_ACTIVATE):
        return

    def _targets(call: ServiceCall) -> list[tuple[SyncManager, str]]:
        index = hass.data.get(DOMAIN, {}).get(DATA_AREA_INDEX, {})
        entity_ids = call.data.get(ATTR_ENTITY_ID) or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        return [index[eid] for eid in entity_ids if eid in index]

    def _overrides(call: ServiceCall) -> dict:
        changes: dict = {}
        if CONF_MODE in call.data:
            changes["mode"] = SyncMode(call.data[CONF_MODE])
        if CONF_EFFECT in call.data:
            changes["effect"] = SyncEffect(call.data[CONF_EFFECT])
        if CONF_COLOUR in call.data:
            changes["colour"] = ColorScheme(call.data[CONF_COLOUR])
        if CONF_BRIGHTNESS in call.data:
            changes["brightness"] = call.data[CONF_BRIGHTNESS] / 100.0
        if CONF_MEDIA_PLAYER in call.data:
            changes["media_player"] = call.data[CONF_MEDIA_PLAYER]
        return changes

    async def _activate(call: ServiceCall) -> None:
        for manager, area_id in _targets(call):
            if changes := _overrides(call):
                await manager.update_settings(area_id, **changes)
            await manager.start_area(area_id)

    async def _deactivate(call: ServiceCall) -> None:
        for manager, area_id in _targets(call):
            await manager.stop_area(area_id)

    async def _set_options(call: ServiceCall) -> None:
        # Apply scheme/effect/player changes live without (re)starting sync.
        for manager, area_id in _targets(call):
            if changes := _overrides(call):
                await manager.update_settings(area_id, **changes)

    options_fields = {
        vol.Optional(CONF_MODE): vol.In([str(m) for m in SyncMode]),
        vol.Optional(CONF_EFFECT): vol.In([str(e) for e in SyncEffect]),
        vol.Optional(CONF_COLOUR): vol.In([str(c) for c in ColorScheme]),
        vol.Optional(CONF_BRIGHTNESS): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=100)
        ),
        vol.Optional(CONF_MEDIA_PLAYER): cv.entity_id,
    }
    activate_schema = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids, **options_fields})
    deactivate_schema = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})
    set_options_schema = vol.Schema(
        {vol.Required(ATTR_ENTITY_ID): cv.entity_ids, **options_fields}
    )

    hass.services.async_register(DOMAIN, SERVICE_ACTIVATE, _activate, activate_schema)
    hass.services.async_register(DOMAIN, SERVICE_DEACTIVATE, _deactivate, deactivate_schema)
    hass.services.async_register(DOMAIN, SERVICE_SET_OPTIONS, _set_options, set_options_schema)
