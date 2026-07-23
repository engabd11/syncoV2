"""The Hue Music Sync integration."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import ssl

import aiohttp
import voluptuous as vol

# Some HA hosts (a misconfigured Windows registry, minimal OS images) map ".js"
# to "text/plain"/"application/octet-stream". Browsers then refuse to *execute*
# the served card as an ES module ("Expected a JavaScript module script but the
# server responded with a MIME type of text/plain"), so the custom element never
# registers and Lovelace shows a generic "Configuration error" — even though the
# file downloads fine and survives every cache clear. Pin the correct type so the
# bundled card is always served as an executable module, on any platform.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APP_KEY,
    CONF_AUTO_LEVELS,
    CONF_AUTO_TIMING,
    CONF_BRIDGE_CERT,
    CONF_BRIGHTNESS,
    CONF_CLIENT_KEY,
    CONF_COLOUR,
    CONF_EFFECT,
    CONF_HOST,
    CONF_MEDIA_PLAYER,
    CONF_MODE,
    CONF_SUBSONIC_PASSWORD,
    CONF_SUBSONIC_URL,
    CONF_SUBSONIC_USER,
    DOMAIN,
    INTENSITY_LADDER,
    PLATFORMS,
    SIGNAL_PREWARM,
    ColorScheme,
    SyncEffect,
    SyncMode,
)
from .coordinator import SyncManager, trackmap_cache_dir, trackmap_cache_stats
from .effects.modes import sanitize_auto_levels
from .hue.bridge import HueBridge, HueBridgeError

_LOGGER = logging.getLogger(__name__)

SERVICE_ACTIVATE = "activate"
SERVICE_DEACTIVATE = "deactivate"
SERVICE_SET_OPTIONS = "set_options"
SERVICE_PREWARM_LIBRARY = "prewarm_library"
SERVICE_ANALYZE_TRACK = "analyze_track"
SERVICE_CLEAR_LIBRARY_CACHE = "clear_library_cache"

ATTR_RETRY_FAILED = "retry_failed"
ATTR_URL = "url"
ATTR_ARTIST = "artist"
ATTR_TITLE = "title"
ATTR_MEDIA_PLAYER = "media_player"
ATTR_FORCE = "force"

DATA_PREWARM_RUNNING = "prewarm_running"
DATA_PREWARM_RESUME = "prewarm_resume_scheduled"
DATA_PREWARM_STATUS = "prewarm_status"

# How long after a config entry loads before an interrupted library pre-warm
# resumes on its own (lets HA finish starting before background analysis).
PREWARM_RESUME_DELAY_S = 120.0

# switch entity_id -> (SyncManager, area_id), populated by switch entities.
DATA_AREA_INDEX = "area_index"
DATA_CARD_REGISTERED = "card_registered"

# The bundled dashboard card is served straight from the integration's own
# ``frontend/`` directory: the file is guaranteed to exist on every install.
# (An earlier build staged a copy into ``config/www`` and loaded it via
# ``/local/`` — but that 404s wherever HA never routed /local (no www folder
# when the frontend loaded) or the copy failed, and it failed SILENTLY: the
# dashboard showed "Custom element doesn't exist" whenever the browser cache
# was cold, and worked whenever a cached copy survived. Serving our own
# directory removes the copy step and the /local dependency entirely; the
# historical MIME concern with integration routes is handled by the
# ``mimetypes.add_type`` pins at the top of this module.)
CARD_FILENAME = "hue-music-sync-card.js"
CARD_BASE_URL = "/hue_music_sync"
CARD_URL = f"{CARD_BASE_URL}/{CARD_FILENAME}"
# The /local URL used by older builds; dashboard-resource entries pointing at
# it are migrated on setup so a stale (potentially 404ing) reference can't
# keep breaking the card.
LOCAL_CARD_URL = f"/local/{CARD_FILENAME}"


def _build_ssl_context(pinned_cert_pem: str | None = None) -> ssl.SSLContext:
    """SSL context for bridge HTTPS (blocking; build in an executor).

    Hue bridges use a self-signed certificate, so CA validation can never
    succeed. With a pinned PEM (captured at pairing, trust-on-first-use) the
    context trusts exactly that certificate and requires it — a LAN
    man-in-the-middle can no longer intercept the CLIP traffic or a pairing's
    app key. Without one (legacy entries before their first re-setup, or a
    capture failure) fall back to unverified TLS as before.
    """
    if pinned_cert_pem:
        ctx = ssl.create_default_context(cadata=pinned_cert_pem)
        ctx.check_hostname = False  # cert carries the bridge id, not the IP
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_bridge_certificate(host: str) -> str | None:
    """Capture the bridge's TLS certificate as PEM (blocking; run in executor)."""
    try:
        return ssl.get_server_certificate((host, 443), timeout=10)
    except (OSError, ssl.SSLError, ValueError):
        return None


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _card_cache_token(card_file: "Path") -> str | None:
    """Content-hash cache-bust token for the card (blocking; run in executor).

    A content hash (not the manifest version) means any card edit invalidates
    the browser cache, so users never have to hard-refresh a new build.
    """
    import hashlib

    try:
        return hashlib.sha256(card_file.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


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
        token = await hass.async_add_executor_job(_card_cache_token, card_file)
        if token is None:
            raise RuntimeError(f"could not read {card_file}")

        # Serve the integration's own frontend/ directory. The file is
        # guaranteed to exist (it ships with the integration) — unlike the old
        # approach of staging a copy into config/www, which 404'd on installs
        # where /local was never routed or the copy failed, and did so
        # SILENTLY: the card then only rendered when a browser happened to
        # have a cached copy, and a cache reset produced "Custom element
        # doesn't exist". If serving fails here, the whole registration fails
        # LOUDLY (the except below) instead of leaving a dead resource URL.
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(CARD_BASE_URL, str(frontend_dir), True)]
            )
        except (ImportError, AttributeError):
            # Older cores without the async API: the classic sync form.
            hass.http.register_static_path(CARD_BASE_URL, str(frontend_dir), True)
        url = f"{CARD_URL}?v={token}"

        # The card is loaded ONLY as a Lovelace resource — deliberately. The
        # obvious second vector, frontend.add_extra_js_url, injects the module
        # into the app shell where it races HA's own core bundle; when the
        # (small, cached) card module wins, it registers its element BEFORE
        # the core bundle installs the scoped custom-element-registry
        # polyfill, whose registry then can't see the definition — every
        # dashboard shows "Custom element doesn't exist" while the module
        # itself ran fine. Resources are imported by the lovelace panel, which
        # is guaranteed to run after the core bundle, so they can never lose
        # that race. (The card itself also defers its define until HA's root
        # element exists, protecting clients with stale cached shells that
        # still carry the old extra_js entry.)
        if not await _register_lovelace_resource(hass, url):
            # Lovelace not ready yet at startup; retry once HA has fully
            # started, and keep retrying briefly — the resource is the ONLY
            # load path that a stale (service-worker-cached) app shell can't
            # break, so giving up after one silent attempt left desktops with
            # an old shell showing "Configuration error" until a cache clear.
            from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

            async def _retry(_event) -> None:
                for delay in (0.0, 5.0, 15.0, 30.0):
                    if delay:
                        await asyncio.sleep(delay)
                    if await _register_lovelace_resource(hass, url):
                        return
                _LOGGER.warning(
                    "Could not register the Hue Synco card as a dashboard "
                    "resource. If your dashboards run in YAML mode, add it "
                    "manually under lovelace: resources: (url: %s, "
                    "type: module); otherwise add it in Settings > Dashboards "
                    "> Resources.",
                    url,
                )

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _retry)
        _LOGGER.info(
            "Hue Synco dashboard card served at %s. If it doesn't appear in the "
            "card picker, reload the page (or clear the browser cache once).",
            url,
        )
    except Exception:  # noqa: BLE001 - never block setup on the card
        hass.data[DOMAIN][DATA_CARD_REGISTERED] = False
        _LOGGER.warning(
            "Could not auto-register the Hue Synco dashboard card; add it manually "
            "as a dashboard resource pointing at %s (JavaScript Module).",
            CARD_URL, exc_info=True,
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
        # Older builds staged the card into config/www and registered it at
        # /local/... — on installs where that never actually served, the entry
        # 404s forever; migrate it away instead of leaving it to break the card.
        legacy_base = LOCAL_CARD_URL
        found = False
        for item in list(resources.async_items()):
            item_base = str(item.get("url", "")).split("?")[0]
            if item_base == base:
                found = True
                if item.get("url") != url:  # version changed: refresh it
                    await resources.async_update_item(item["id"], {"url": url})
            elif item_base == legacy_base:
                try:
                    await resources.async_delete_item(item["id"])
                    _LOGGER.info("Removed stale Hue Synco card resource %s", item_base)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    _LOGGER.debug("Could not remove stale card resource", exc_info=True)
        if not found:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.debug("Added Hue Synco card as a Lovelace resource: %s", url)
        return True
    except Exception:  # noqa: BLE001 - best-effort
        _LOGGER.debug("Lovelace resource registration failed", exc_info=True)
        return False


# No YAML configuration: everything is set up from config entries (async_setup
# only pre-registers the card/WS feed). Declared for hassfest's CONFIG_SCHEMA
# check.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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

    # A library pre-warm interrupted by a restart resumes on its own (it is
    # resumable by design: already-cached tracks are skipped). Scheduled once,
    # delayed so HA finishes starting before background analysis begins.
    if not hass.data[DOMAIN].get(DATA_PREWARM_RESUME):
        hass.data[DOMAIN][DATA_PREWARM_RESUME] = True

        async def _resume_prewarm(_now) -> None:
            if hass.data.get(DOMAIN, {}).get(DATA_PREWARM_RUNNING):
                return
            state = await hass.async_add_executor_job(_read_prewarm_state, hass)
            if state is not None and not state.get("completed"):
                _LOGGER.info(
                    "Resuming the interrupted library pre-warm (%s tracks total)",
                    state.get("total", "?"),
                )
                await _start_library_prewarm(hass)

        from homeassistant.helpers.event import async_call_later

        async_call_later(hass, PREWARM_RESUME_DELAY_S, _resume_prewarm)

    session = async_get_clientsession(hass)
    host = entry.data[CONF_HOST]
    # Certificate pinning (trust-on-first-use): entries created before pinning
    # existed capture the bridge's certificate on their next setup; from then
    # on every CLIP call verifies it's the same bridge.
    pinned_cert = entry.data.get(CONF_BRIDGE_CERT)
    if not pinned_cert:
        pinned_cert = await hass.async_add_executor_job(_fetch_bridge_certificate, host)
        if pinned_cert:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_BRIDGE_CERT: pinned_cert}
            )
            _LOGGER.info("Pinned the Hue bridge's TLS certificate (trust-on-first-use)")
        else:
            _LOGGER.warning(
                "Could not capture the Hue bridge's TLS certificate; "
                "connecting without verification for now"
            )
    ssl_ctx = await hass.async_add_executor_job(_build_ssl_context, pinned_cert)
    bridge = HueBridge(session, host, entry.data[CONF_APP_KEY], ssl_ctx)

    try:
        configs = await bridge.get_entertainment_configs()
    except (HueBridgeError, OSError) as err:
        if isinstance(err, aiohttp.ClientConnectorCertificateError):
            # The bridge presented a different certificate than the pinned
            # one. Legitimate cause: a factory-reset/replaced bridge (which
            # invalidates the app key anyway). Malicious cause: an on-path
            # interceptor. Never silently re-pin — tell the user instead.
            raise ConfigEntryNotReady(
                f"Hue bridge {host} presented an unexpected TLS certificate. "
                "If the bridge was factory-reset or replaced, remove and "
                "re-add the integration to pair (and pin) it again."
            ) from err
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
        # Last loaded entry gone: remove the domain services so nothing
        # dangles. (The WebSocket commands have no unregister API; their
        # handlers already resolve targets safely against hass.data.)
        if not any(
            e.entry_id in hass.data[DOMAIN]
            for e in hass.config_entries.async_entries(DOMAIN)
        ):
            for service in (
                SERVICE_ACTIVATE,
                SERVICE_DEACTIVATE,
                SERVICE_SET_OPTIONS,
                SERVICE_PREWARM_LIBRARY,
            ):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
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
        if CONF_AUTO_LEVELS in call.data:
            changes["auto_levels"] = sanitize_auto_levels(call.data[CONF_AUTO_LEVELS])
        if CONF_AUTO_TIMING in call.data:
            changes["auto_timing"] = bool(call.data[CONF_AUTO_TIMING])
        if CONF_EFFECT in call.data:
            changes["effect"] = SyncEffect(call.data[CONF_EFFECT])
        if CONF_COLOUR in call.data:
            changes["colour"] = ColorScheme(call.data[CONF_COLOUR])
        if CONF_BRIGHTNESS in call.data:
            changes["brightness"] = call.data[CONF_BRIGHTNESS] / 100.0
        if CONF_MEDIA_PLAYER in call.data:
            # An empty value clears the pin and returns the area to auto-picking
            # whatever is playing. Normalise "" to None so the stored setting has
            # exactly one "no player" value (an empty string would read as a
            # change every time and churn a source reset).
            changes["media_player"] = call.data[CONF_MEDIA_PLAYER] or None
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
        # The rungs Auto may pick from (only meaningful with mode=auto).
        vol.Optional(CONF_AUTO_LEVELS): vol.All(
            cv.ensure_list, [vol.In([str(m) for m in INTENSITY_LADDER])]
        ),
        # Opt-in per-song timing auto-calibration (only meaningful with a live tap).
        vol.Optional(CONF_AUTO_TIMING): cv.boolean,
        vol.Optional(CONF_EFFECT): vol.In([str(e) for e in SyncEffect]),
        vol.Optional(CONF_COLOUR): vol.In([str(c) for c in ColorScheme]),
        vol.Optional(CONF_BRIGHTNESS): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=100)
        ),
        # "" / None means "no pinned player": follow whatever is playing.
        vol.Optional(CONF_MEDIA_PLAYER): vol.Any(None, "", cv.entity_id),
    }
    activate_schema = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids, **options_fields})
    deactivate_schema = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})
    set_options_schema = vol.Schema(
        {vol.Required(ATTR_ENTITY_ID): cv.entity_ids, **options_fields}
    )

    async def _prewarm_library(call: ServiceCall) -> None:
        await _start_library_prewarm(
            hass,
            retry_failed=bool(call.data.get(ATTR_RETRY_FAILED)),
            force=bool(call.data.get(ATTR_FORCE)),
        )

    async def _clear_library_cache(call: ServiceCall) -> None:
        await _clear_library_cache_impl(hass)

    async def _analyze_track(call: ServiceCall) -> None:
        hass.async_create_background_task(
            _run_analyze_track(hass, dict(call.data)), "hue_music_sync_analyze_track"
        )

    hass.services.async_register(DOMAIN, SERVICE_ACTIVATE, _activate, activate_schema)
    hass.services.async_register(DOMAIN, SERVICE_DEACTIVATE, _deactivate, deactivate_schema)
    hass.services.async_register(DOMAIN, SERVICE_SET_OPTIONS, _set_options, set_options_schema)
    hass.services.async_register(
        DOMAIN,
        SERVICE_PREWARM_LIBRARY,
        _prewarm_library,
        vol.Schema(
            {
                vol.Optional(ATTR_RETRY_FAILED, default=False): cv.boolean,
                # force: wipe the whole cache first and re-analyse every track
                # (e.g. to upgrade all maps to a newer analysis format).
                vol.Optional(ATTR_FORCE, default=False): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_LIBRARY_CACHE,
        _clear_library_cache,
        vol.Schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ANALYZE_TRACK,
        _analyze_track,
        vol.Schema(
            {
                vol.Optional(ATTR_URL): cv.string,
                vol.Optional(ATTR_ARTIST): cv.string,
                vol.Optional(ATTR_TITLE): cv.string,
                vol.Optional(ATTR_MEDIA_PLAYER): cv.entity_id,
                vol.Optional(ATTR_FORCE, default=True): cv.boolean,
            }
        ),
    )


def _prewarm_state_path(hass: HomeAssistant) -> str:
    import os

    return os.path.join(trackmap_cache_dir(hass), "prewarm_state.json")


def _read_prewarm_state(hass: HomeAssistant) -> dict | None:
    """Blocking (executor). The persisted progress of the library pre-warm."""
    import json

    try:
        with open(_prewarm_state_path(hass), encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else None
    except (OSError, ValueError):
        return None


def _write_prewarm_state(hass: HomeAssistant, state: dict) -> None:
    """Blocking (executor). Persist pre-warm progress so a restart resumes it."""
    import json
    import os

    path = _prewarm_state_path(hass)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        _LOGGER.debug("Could not persist pre-warm state", exc_info=True)


def _clear_trackmap_cache(hass: HomeAssistant) -> tuple[int, int]:
    """Delete every cached track map + the sweep bookkeeping. Blocking (executor).

    Returns ``(maps_removed, bytes_freed)`` — only the ``.npz`` maps are counted;
    the small index/report/state files are removed too so nothing is left behind.
    """
    import os

    meta = {"track_index.json", "analysis_report.json", "prewarm_state.json"}
    removed = 0
    freed = 0
    try:
        with os.scandir(trackmap_cache_dir(hass)) as it:
            entries = list(it)
    except OSError:
        return 0, 0
    for e in entries:
        if not (e.name.endswith(".npz") or e.name in meta):
            continue
        try:
            sz = e.stat().st_size
        except OSError:
            sz = 0
        try:
            os.unlink(e.path)
        except OSError:
            continue
        if e.name.endswith(".npz"):
            removed += 1
            freed += sz
    return removed, freed


async def _clear_library_cache_impl(hass: HomeAssistant) -> tuple[int, int]:
    """Wipe the whole library cache: on-disk maps + the shared index + status."""
    from .coordinator import get_track_index

    if hass.data.setdefault(DOMAIN, {}).get(DATA_PREWARM_RUNNING):
        _LOGGER.warning(
            "Clearing the library cache while a pre-warm sweep is running; the "
            "sweep may re-create some maps — run it again after it finishes."
        )
    index = get_track_index(hass)
    await index.ensure_loaded()
    index.clear_all()
    removed, freed = await hass.async_add_executor_job(_clear_trackmap_cache, hass)
    _prewarm_update(
        hass, status="never run", running=False, total=0, done=0,
        analysed=0, ambient=0, failed=0, pending=None, last_run=None,
        last_error=None, failed_tracks=[],
    )
    _LOGGER.info(
        "Library cache cleared: %d maps removed (%.1f MB freed)",
        removed, freed / (1024 * 1024),
    )
    return removed, freed


def prewarm_status(hass: HomeAssistant) -> dict:
    """The live library-analysis status shared with the button/sensor entities."""
    data = hass.data.setdefault(DOMAIN, {})
    return data.setdefault(
        DATA_PREWARM_STATUS,
        {
            "status": "never run",
            "running": False,
            "total": 0,
            "done": 0,
            "analysed": 0,
            "ambient": 0,
            "failed": 0,
            "pending": None,
            "last_run": None,
            "last_error": None,
            "failed_tracks": [],
        },
    )


def _prewarm_update(hass: HomeAssistant, **changes) -> None:
    """Apply status changes and wake every subscribed entity."""
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    prewarm_status(hass).update(changes)
    async_dispatcher_send(hass, SIGNAL_PREWARM)


def _prewarm_notify(hass: HomeAssistant, message: str) -> None:
    """Progress/result surface the user can actually see (HA notification)."""
    hass.async_create_task(
        hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Hue Synco — library analysis",
                "message": message,
                "notification_id": "hue_music_sync_prewarm",
            },
        )
    )


def _subsonic_cfg(hass: HomeAssistant):
    """(url, user, password) from the first entry that configured a library, or None."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        url = (entry.options.get(CONF_SUBSONIC_URL) or "").strip()
        if url:
            return (
                url,
                entry.options.get(CONF_SUBSONIC_USER, ""),
                entry.options.get(CONF_SUBSONIC_PASSWORD, ""),
            )
    return None


def _report_path(hass: HomeAssistant) -> str:
    import os

    return os.path.join(trackmap_cache_dir(hass), "analysis_report.json")


async def _start_library_prewarm(
    hass: HomeAssistant, retry_failed: bool = False, force: bool = False
) -> None:
    """Analyse the whole Music Assistant library into the on-disk cache.

    Runs once, in the background, gently (one track at a time, yielding to live
    playback analysis), so every track plays instantly with offline track-map
    reaction the first time too — not just on a repeat or in a queue. Resumable
    AND incremental: already-cached tracks are skipped, so re-running continues
    where it left off and picks up newly added library tracks cheaply.
    ``retry_failed`` clears the persistent failure verdicts (and deletes
    ambient-tier maps) first, so everything problematic re-analyses — the way
    to benefit from an analysis upgrade. ``force`` goes further and wipes the
    WHOLE cache first, so every track (even a usable, up-to-date map) is
    re-analysed — the way to upgrade the entire library to a new map format.
    """
    from .audio.source import library_prewarm_items
    from .audio.trackmap import TrackMapper
    from .coordinator import get_track_index

    import time as _time

    data = hass.data.setdefault(DOMAIN, {})
    if data.get(DATA_PREWARM_RUNNING):
        _LOGGER.info("Library pre-warm is already running")
        return
    data[DATA_PREWARM_RUNNING] = True
    _prewarm_update(
        hass, status="finding tracks", running=True,
        done=0, analysed=0, ambient=0, failed=0, last_error=None,
    )

    throttle = {"sensor": 0.0, "notify": 0.0}

    def _progress(analysed: int, considered: int, total: int) -> None:
        now = _time.monotonic()
        # Live sensor: at most ~1/s (skipped cached tracks sweep very fast).
        if now - throttle["sensor"] >= 1.0 or considered >= total:
            throttle["sensor"] = now
            _prewarm_update(hass, done=considered, analysed=analysed)
        # Notification: a slow secondary surface.
        if now - throttle["notify"] >= 60.0:
            throttle["notify"] = now
            pct = round(100 * considered / max(1, total))
            _LOGGER.info(
                "Library pre-warm progress: %d of %d tracks (%d%%), %d newly analysed",
                considered, total, pct, analysed,
            )
            _prewarm_notify(
                hass,
                f"Analysing your music library so every song reacts from the "
                f"first beat: {pct}% ({considered} of {total} tracks checked, "
                f"{analysed} newly analysed). Runs gently in the background "
                f"and survives restarts.",
            )

    async def _run() -> None:
        index = get_track_index(hass)
        await index.ensure_loaded()
        mapper = TrackMapper(
            _ffmpeg_binary(hass),
            spawner=lambda coro, name: hass.async_create_background_task(coro, name),
            cache_dir=trackmap_cache_dir(hass),
            track_index=index,
        )
        try:
            if force:
                # Full re-analysis: wipe every map + record so has_disk() misses
                # on all of them and the whole library is analysed fresh.
                index.clear_all()
                await hass.async_add_executor_job(_clear_trackmap_cache, hass)
                _LOGGER.info(
                    "Library pre-warm: force — cleared the whole cache for a "
                    "full re-analysis"
                )
            if retry_failed:
                cleared = index.clear_failures()
                ambient = index.ambient_keys()
                # Delete ambient-tier maps so the (possibly upgraded) analysis
                # gets a fresh shot at a full beat grid for them.
                if ambient:
                    import os

                    def _drop_ambient() -> None:
                        for key in ambient:
                            try:
                                os.unlink(
                                    os.path.join(trackmap_cache_dir(hass), f"{key}.npz")
                                )
                            except OSError:
                                pass

                    await hass.async_add_executor_job(_drop_ambient)
                _LOGGER.info(
                    "Library pre-warm: retry_failed cleared %d failure records "
                    "and %d ambient maps for re-analysis", cleared, len(ambient),
                )
            # Enumerate inside the task (paged library calls can take a moment)
            # so the service call returns immediately.
            items = await library_prewarm_items(hass, _subsonic_cfg(hass))
            if not items:
                _LOGGER.warning(
                    "Library pre-warm found no analysable tracks. For a Navidrome "
                    "/ OpenSubsonic library, set the library URL + login in the "
                    "integration options so stream URLs can be built."
                )
                _prewarm_notify(
                    hass,
                    "No analysable tracks were found in the Music Assistant "
                    "library. For a Navidrome/OpenSubsonic library, set the "
                    "library URL + login in the Hue Synco integration options, "
                    "then run the `hue_music_sync.prewarm_library` service again.",
                )
                _prewarm_update(hass, status="no analysable tracks", running=False)
                return
            total = len(items)
            # Size the shared disk cache to the library BEFORE analysing, so
            # the prune can never evict maps the sweep just wrote.
            index.set_library_total(total)
            # How many enumerated tracks have no map yet (the "new since last
            # run" signal; permanent failures are not pending, they're failed).
            def _count_pending() -> int:
                return sum(
                    1 for sig, _url, _label in items
                    if not mapper.has_disk(sig) and not mapper.failed(sig)
                )

            pending = await hass.async_add_executor_job(_count_pending)
            _LOGGER.info(
                "Library pre-warm starting: %d tracks total, %d to analyse",
                total, pending,
            )
            _prewarm_update(hass, status="running", total=total, pending=pending)
            # Mark the sweep in-flight so an HA restart resumes it automatically.
            await hass.async_add_executor_job(
                _write_prewarm_state, hass, {"completed": False, "total": total}
            )
            res = await mapper.prewarm(
                items, delay_s=1.0, progress=lambda a, c: _progress(a, c, total)
            )
            interrupted = res.considered < total  # stop_prewarm() ended it early
            now_iso = dt_util.utcnow().isoformat()
            await hass.async_add_executor_job(
                _write_prewarm_state,
                hass,
                {
                    "completed": not interrupted,
                    "total": total,
                    "analysed": res.analysed,
                    "ambient": res.ambient,
                    "failed": res.failed,
                    "finished_at": now_iso,
                    "disk_budget": index.disk_budget,
                },
            )
            # The full problem-track report (uncapped, with reasons) next to
            # the cache, for anything the sensor attribute cannot carry.
            await hass.async_add_executor_job(
                index.write_report_sync, _report_path(hass)
            )
            first = res.failures[0] if res.failures else None
            _prewarm_update(
                hass,
                status="interrupted" if interrupted else "complete",
                running=False,
                done=res.considered,
                analysed=res.analysed,
                ambient=res.ambient,
                failed=res.failed,
                pending=max(
                    0, pending - res.analysed - res.ambient - res.failed
                ) if not interrupted else None,
                last_run=now_iso,
                last_error=(f"{first[0]} ({first[2]})" if first else None),
                failed_tracks=index.failed_entries(25),
            )
            _LOGGER.info(
                "Library pre-warm complete: %d newly analysed (+%d ambient-only) "
                "of %d tracks (the rest were already cached, failed, or not "
                "analysable)", res.analysed, res.ambient, total,
            )
            skipped = res.considered - res.analysed - res.ambient - res.failed
            msg = (
                f"Library analysis finished: {res.analysed} newly analysed"
            )
            if res.ambient:
                msg += (
                    f" plus {res.ambient} ambient-only (features/colours, "
                    f"no reliable beat grid)"
                )
            msg += f", {max(0, skipped)} already cached or skipped"
            if res.failed:
                lines = "\n".join(
                    f"- {label} ({err})" for label, _url, err in res.failures[:10]
                )
                msg += (
                    f", **{res.failed} failed**:\n{lines}\n\n"
                    f"The full list is in "
                    f"`hue_music_sync/trackmaps/analysis_report.json`. Diagnose "
                    f"one song with the `hue_music_sync.analyze_track` service, "
                    f"or re-try everything with `prewarm_library` and "
                    f"`retry_failed: true`. If most tracks failed, check the "
                    f"OpenSubsonic/Navidrome URL + login in the integration "
                    f"options."
                )
            else:
                msg += ". Every analysed song now reacts from the first beat."
            _prewarm_notify(hass, msg)
        except asyncio.CancelledError:
            _LOGGER.info("Library pre-warm cancelled; will resume on next start")
            _prewarm_update(hass, status="interrupted", running=False)
        except Exception:  # noqa: BLE001 - never let the sweep crash HA
            _LOGGER.exception("Library pre-warm failed")
            _prewarm_update(hass, status="failed — see the log", running=False)
        finally:
            data[DATA_PREWARM_RUNNING] = False
            await mapper.close()

    hass.async_create_background_task(_run(), "hue_music_sync_prewarm")


async def _run_analyze_track(hass: HomeAssistant, params: dict) -> None:
    """The ``analyze_track`` service: diagnose (and re-analyse) ONE song.

    Resolves the track from a direct URL, an artist+title library lookup, or
    whatever a media_player is currently playing; runs the offline analysis
    right away (``force`` clears any failure verdict first) and posts the full
    structured diagnostics as a persistent notification — the per-song answer
    to "why doesn't this track react".
    """
    from .audio.library_match import TrackQuery
    from .audio.source import (
        ha_track_query,
        library_index,
        resolve_map_url,
        track_label,
        track_signature,
    )
    from .audio.trackmap import TrackMapper
    from .coordinator import get_track_index

    def _notify(message: str) -> None:
        hass.async_create_task(
            hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Hue Synco — track analysis",
                    "message": message,
                    "notification_id": "hue_music_sync_analyze",
                },
            )
        )

    subsonic = _subsonic_cfg(hass)
    url = (params.get(ATTR_URL) or "").strip() or None
    artist = (params.get(ATTR_ARTIST) or "").strip() or None
    title = (params.get(ATTR_TITLE) or "").strip() or None
    entity_id = params.get(ATTR_MEDIA_PLAYER)
    force = bool(params.get(ATTR_FORCE, True))

    sig = label = None
    try:
        if url:
            label = "direct URL"
        elif title or artist:
            entry = await library_index(hass, subsonic).async_lookup(
                TrackQuery(title=title, artist=artist)
            )
            if entry is None:
                _notify(
                    f"No Music Assistant library track matched "
                    f"**{track_label(artist, title)}**. Check the spelling, or "
                    f"pass the stream `url` directly."
                )
                return
            sig, url = entry.signature, entry.url
            label = track_label(entry.artist, entry.title)
        elif entity_id:
            st = hass.states.get(entity_id)
            attrs = st.attributes if st else {}
            sig = track_signature(
                attrs.get("media_content_id"),
                attrs.get("media_artist"),
                attrs.get("media_title"),
            )
            label = track_label(attrs.get("media_artist"), attrs.get("media_title"))
            url = resolve_map_url(hass, entity_id, subsonic)
            if url is None:
                entry = await library_index(hass, subsonic).async_lookup(
                    ha_track_query(hass, entity_id)
                )
                if entry is not None:
                    sig, url = entry.signature, entry.url
        else:
            _notify(
                "Nothing to analyse: pass a `url`, an `artist` + `title`, or a "
                "`media_player` that is currently playing."
            )
            return
        if not url:
            _notify(
                f"**{label}**: no analysable stream URL could be resolved. For a "
                f"Navidrome/OpenSubsonic library, set the library URL + login in "
                f"the integration options."
            )
            return

        mapper = TrackMapper(
            _ffmpeg_binary(hass),
            spawner=lambda coro, name: hass.async_create_background_task(coro, name),
            cache_dir=trackmap_cache_dir(hass),
            track_index=get_track_index(hass),
        )
        _LOGGER.info("Analysing %s on request (analyze_track)", label)
        result = await mapper.analyze_now(sig, url, label, force=force)
        _notify(_format_analysis(label, result, cached=sig is not None))
    except Exception:  # noqa: BLE001 - a diagnostics service must never crash HA
        _LOGGER.exception("analyze_track failed")
        _notify("Track analysis crashed — see the Home Assistant log.")


def _format_analysis(label: str | None, result, cached: bool) -> str:
    """Human summary of a MapResult + MapDiagnostics for the notification."""
    tm = result.track_map
    d = result.diagnostics
    lines = [f"**{label or '?'}**"]
    if tm is None:
        lines.append(
            f"Analysis FAILED: {result.error or 'unknown error'} "
            f"({'stream decoded' if result.decoded else 'could not fetch/decode the stream'})."
        )
        if not result.decoded:
            lines.append(
                "This is a fetch problem (URL/login/network), not a music "
                "problem — check the OpenSubsonic/Navidrome settings."
            )
    else:
        tier = d.tier if d else ("full" if tm.grid_usable else "ambient")
        if tier == "full":
            lines.append(
                "Result: **full tier** — scheduled beat-grid playback. "
                "This song gets the complete show."
            )
        else:
            lines.append(
                "Result: **ambient tier** — energy/colours/sections drive the "
                "lights and the live beat tracker follows the replayed onsets, "
                "but no offline beat schedule was trustworthy."
            )
        if d is not None:
            lines.append(
                f"- analysed {d.analysed_s:.0f} s, {d.bpm:.1f} BPM, "
                f"confidence {d.confidence:.2f} (autocorrelation {d.autocorr_conf:.2f})"
            )
            lines.append(
                f"- beats {d.beats}, sections {d.sections}, "
                f"beats-on-peaks contrast {d.contrast:.1f} (noise is ~2.9), "
                f"interval spread {d.mad_local:.2f}, coverage {d.coverage:.0%}, "
                f"tempo stability {d.tempo_stability:.0%}"
            )
            if d.reason:
                lines.append(f"- reason: {d.reason}")
        if cached:
            lines.append(
                "The map was saved: this song now plays with track-map sync."
            )
    return "\n".join(lines)
