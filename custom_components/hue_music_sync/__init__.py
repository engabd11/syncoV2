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


def _card_cache_token(card_file: "Path") -> str:
    """A cache-bust token that changes whenever the card file's bytes change.

    Using a content hash (not the integration's manifest version) means an edited
    card always invalidates the browser / HA service-worker cache, so users never
    have to hard-refresh or restart to pick up a new card build.
    """
    import hashlib

    try:
        digest = hashlib.sha256(card_file.read_bytes()).hexdigest()
        return digest[:12]
    except OSError:
        # Fall back to mtime if the file can't be read for some reason.
        try:
            return str(int(card_file.stat().st_mtime))
        except OSError:
            return "0"


async def _register_frontend_card(hass: HomeAssistant) -> None:
    """Serve and register the bundled dashboard card once.

    Installing the integration then provides the Hue Synco Card automatically —
    no manual dashboard-resource step. Best-effort: a failure here never blocks
    the integration (the card can still be added as a resource by hand).
    """
    from pathlib import Path

    hass.data.setdefault(DOMAIN, {})
    if hass.data[DOMAIN].get(DATA_CARD_REGISTERED):
        return
    frontend_dir = Path(__file__).parent / "frontend"
    card_file = frontend_dir / "hue-music-sync-card.js"
    if not card_file.is_file():
        _LOGGER.error(
            "Hue Synco card file is missing under %s; reinstall/update the "
            "integration so the bundled card ships with it", frontend_dir
        )
        return
    hass.data[DOMAIN][DATA_CARD_REGISTERED] = True
    try:
        from homeassistant.components.frontend import add_extra_js_url

        # Serve the whole frontend/ directory (directory static serving is the
        # most robust form), with a legacy fallback for older HA cores.
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_BASE_URL, str(frontend_dir), False)]
            )
        except (ImportError, AttributeError):
            hass.http.register_static_path(CARD_BASE_URL, str(frontend_dir), False)

        # Cache-bust on the card file's content hash (computed off the event loop),
        # so an updated card is always re-fetched without a manual hard refresh.
        token = await hass.async_add_executor_job(_card_cache_token, card_file)
        url = f"{CARD_URL}?v={token}"

        # Two ways to load the card, so it works regardless of dashboard mode and
        # browser/app-shell caching:
        #  1. A Lovelace resource (storage mode) — the canonical mechanism, loaded
        #     by the dashboard at runtime so it isn't blocked by a cached shell.
        #  2. add_extra_js_url — covers YAML-mode dashboards.
        add_extra_js_url(hass, url)
        if not await _register_lovelace_resource(hass, url):
            # Lovelace not ready yet at startup; retry once HA has fully started.
            from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

            async def _retry(_event) -> None:
                await _register_lovelace_resource(hass, url)

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)
        _LOGGER.info(
            "Hue Synco dashboard card registered at %s. If it doesn't appear in "
            "the card picker, reload the page (or clear the browser cache once).",
            url,
        )
    except Exception:  # noqa: BLE001 - never block setup on the card
        hass.data[DOMAIN][DATA_CARD_REGISTERED] = False
        _LOGGER.warning(
            "Could not auto-register the Hue Synco dashboard card; add it manually "
            "as a dashboard resource pointing at %s (JavaScript Module).", CARD_URL,
            exc_info=True,
        )


async def _register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Add (or update) the card as a Lovelace resource in storage mode.

    Returns True if the resource is present afterward, False if Lovelace isn't
    ready yet or runs in YAML mode (where resources are managed in YAML).
    """
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None or not hasattr(resources, "async_create_item"):
        return False
    try:
        await resources.async_get_info()  # ensure the collection is loaded
        base = url.split("?")[0]
        for item in resources.async_items():
            if str(item.get("url", "")).split("?")[0] == base:
                if item.get("url") != url:  # version changed: refresh it
                    await resources.async_update_item(item["id"], {"url": url})
                return True
        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.debug("Added Hue Synco card as a Lovelace resource: %s", url)
        return True
    except Exception:  # noqa: BLE001 - best-effort
        _LOGGER.debug("Lovelace resource registration failed", exc_info=True)
        return False


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Component setup: register the bundled card + live feed as early as possible.

    Runs on HA start before any config entry is set up (and regardless of whether
    the Hue bridge is reachable), so the dashboard resource is in place before the
    frontend loads. This is the key fix for the card needing several refreshes /
    a restart to appear: previously registration only happened in
    ``async_setup_entry``, which runs late and can be delayed by bridge retries.
    """
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_AREA_INDEX, {})
    await _register_frontend_card(hass)

    # Live feed for the bundled card (real visualizer / room mirror / timeline).
    from .ws import async_register_ws

    async_register_ws(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hue Synco from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_AREA_INDEX, {})
    # Idempotent fallback: async_setup already did these on HA start, but a config
    # entry added at runtime (fresh install) gets the card/feed registered here too.
    await _register_frontend_card(hass)

    # Live feed for the bundled card (real visualizer / room mirror / timeline).
    from .ws import async_register_ws

    async_register_ws(hass)

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
