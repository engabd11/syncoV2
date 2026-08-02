"""Config flow: pair with the bridge and choose entertainment areas."""

from __future__ import annotations

import logging
import ssl
import time
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BACKEND_MA,
    BACKEND_SUBSONIC,
    CONF_ACTIVE_BACKEND,
    CONF_APP_KEY,
    CONF_AREAS,
    CONF_BRIDGE_CERT,
    CONF_BRIDGE_ID,
    CONF_CLIENT_KEY,
    CONF_HOST,
    CONF_RESTORE_LIGHTS,
    CONF_SNAPSERVER_HOST,
    CONF_SUBSONIC_PASSWORD,
    CONF_SUBSONIC_URL,
    CONF_SUBSONIC_USER,
    DATA_DISCOVERY_CACHE,
    DEFAULT_BACKEND,
    DEFAULT_NAME,
    DEFAULT_RESTORE_LIGHTS,
    DISCOVERY_CACHE_TTL,
    DOMAIN,
)
from .hue.bridge import (
    HueBridge,
    HueBridgeError,
    LinkButtonNotPressed,
    create_app_key,
)
from .hue.certs import cert_common_name, matches_bridge_id

_LOGGER = logging.getLogger(__name__)


_DISCOVERY_URL = "https://discovery.meethue.com"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _devicetype(hass) -> str:
    """The ``devicetype`` this install registers on the bridge's whitelist.

    Core Concepts: *"Each new instance of an app should use a unique application
    key"*. The documented format is ``<application>#<device>`` and it is what
    the user sees when reviewing connected apps in the Hue app, so naming the
    Home Assistant instance makes a multi-HA household readable instead of
    showing several identical entries. Falls back to the plain default when the
    location name is empty or unusable.
    """
    raw = (getattr(hass.config, "location_name", "") or "").strip()
    # The bridge rejects awkward characters here; keep it to a safe subset and
    # inside the field's length limit.
    cleaned = "".join(c for c in raw if c.isalnum() or c in " -_")[:20].strip()
    return f"{DEFAULT_NAME}-{cleaned.replace(' ', '_')}" if cleaned else DEFAULT_NAME


async def _fetch_bridge_id(session, host: str, ssl_ctx) -> str | None:
    """Read the bridge id from the unauthenticated config endpoint.

    Bridge Discovery: *"It is advisable to validate information from these
    bridges before giving the user the option to select one… This can be
    executed by sending a GET https://<bridge ip address>/api/0/config"*. The
    endpoint needs no application key and returns no secret, so it is safe to
    read before certificate validation is possible — which is the point, since
    the bridge id is what the certificate must then be validated against.
    """
    try:
        async with session.get(
            f"https://{host}/api/0/config", ssl=ssl_ctx, timeout=_TIMEOUT
        ) as resp:
            data = await resp.json(content_type=None)
        return data.get("bridgeid")
    except Exception:  # noqa: BLE001
        return None


async def _discover_bridges(hass) -> dict[str, str]:
    """Ask the Hue cloud which bridges live on this network: ``{id: ip}``.

    The documented fallback when mDNS finds nothing. It only works because the
    bridge and this host share a public IP. The endpoint is rate limited to
    *"maximum one request per 15 minutes per client"*, so results are cached in
    ``hass.data`` and the discovery step is never allowed to hammer it.
    """
    cache = hass.data.setdefault(DOMAIN, {}).setdefault(DATA_DISCOVERY_CACHE, {})
    now = time.monotonic()
    if cache.get("at", 0.0) and now - cache["at"] < DISCOVERY_CACHE_TTL:
        return cache.get("bridges", {})
    bridges: dict[str, str] = {}
    try:
        session = async_get_clientsession(hass)
        async with session.get(_DISCOVERY_URL, timeout=_TIMEOUT) as resp:
            resp.raise_for_status()
            for item in await resp.json(content_type=None):
                bid = item.get("id")
                ip = item.get("internalipaddress")
                if bid and ip:
                    bridges[bid] = ip
    except Exception as err:  # noqa: BLE001 - discovery is best-effort
        _LOGGER.debug("Hue cloud discovery failed: %s", err)
    cache["at"] = now
    cache["bridges"] = bridges
    return bridges


class HueMusicSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Hue Music Sync."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._app_key: str | None = None
        self._client_key: str | None = None
        self._bridge_id: str | None = None
        self._bridge_cert: str | None = None
        self._configs: dict[str, str] = {}
        self._discovery_ctx: ssl.SSLContext | None = None
        self._paired_ctx: ssl.SSLContext | None = None

    async def _discovery_ssl(self) -> ssl.SSLContext:
        """Unverified context, used only to read the bridge id and certificate."""
        if self._discovery_ctx is None:
            from . import _build_unverified_context

            self._discovery_ctx = await self.hass.async_add_executor_job(
                _build_unverified_context
            )
        return self._discovery_ctx

    async def _verified_ssl(self) -> ssl.SSLContext:
        """Validating context used for pairing and every call after it.

        Built once the bridge's certificate is in hand, so the pairing POST —
        the one request that ever carries the ``clientkey`` — is protected. The
        "Using HTTPS" guide is explicit that unverified TLS "must never be used
        in production", and pairing is exactly when it would cost the most.
        """
        if self._paired_ctx is None:
            from . import _build_ssl_context

            self._paired_ctx = await self.hass.async_add_executor_job(
                _build_ssl_context, self._bridge_cert
            )
        return self._paired_ctx

    async def async_step_zeroconf(self, discovery_info) -> ConfigFlowResult:
        """A bridge announced itself over mDNS (``_hue._tcp.local.``).

        The preferred discovery method in the Bridge Discovery guide. The
        service's TXT record carries the bridge id, which is also this entry's
        unique id — so a bridge that changed IP updates the existing entry
        rather than appearing as a duplicate.
        """
        host = discovery_info.host
        properties = {
            str(k).lower(): str(v) for k, v in (discovery_info.properties or {}).items()
        }
        bridge_id = properties.get("bridgeid")
        if not bridge_id:
            return self.async_abort(reason="not_hue_bridge")
        # mDNS reports the bridge id lower-cased while /api/config reports it
        # upper-cased, and entries created through the manual step used the
        # latter verbatim. Reuse an existing entry's exact spelling so a
        # discovered bridge updates it instead of appearing as a second one.
        self._bridge_id = self._existing_unique_id(bridge_id) or bridge_id.upper()
        self._host = host
        await self.async_set_unique_id(self._bridge_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_link()

    def _existing_unique_id(self, bridge_id: str) -> str | None:
        """An already-configured entry's unique id for this bridge, if any."""
        wanted = bridge_id.strip().lower()
        for entry in self._async_current_entries():
            if entry.unique_id and entry.unique_id.strip().lower() == wanted:
                return entry.unique_id
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            session = async_get_clientsession(self.hass)
            self._bridge_id = await _fetch_bridge_id(
                session, self._host, await self._discovery_ssl()
            )
            if not self._bridge_id:
                errors["base"] = "cannot_connect"
            else:
                # Match an existing entry's spelling if there is one (see
                # _existing_unique_id) so re-adding a bridge aborts as a
                # duplicate rather than silently creating a twin.
                self._bridge_id = (
                    self._existing_unique_id(self._bridge_id) or self._bridge_id
                )
                await self.async_set_unique_id(self._bridge_id)
                self._abort_if_unique_id_configured()
                return await self.async_step_link()

        # Offer whatever the cloud endpoint knows about this network as the
        # default, so the manual step is usually a confirmation rather than a
        # trip to the router's DHCP table.
        suggested = ""
        if not user_input:
            bridges = await _discover_bridges(self.hass)
            configured = {
                e.unique_id for e in self._async_current_entries() if e.unique_id
            }
            for bid, ip in bridges.items():
                if bid.lower() not in configured:
                    suggested = ip
                    break

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=suggested): str}
                if suggested
                else {vol.Required(CONF_HOST): str}
            ),
            errors=errors,
        )

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the user to press the bridge link button, then pair."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            # Capture and check the certificate *before* pairing: the pairing
            # response carries the clientkey (the DTLS pre-shared key), which
            # the bridge will never reveal again, so it must not cross an
            # unvalidated channel even once.
            from . import _fetch_bridge_certificate

            self._bridge_cert = await self.hass.async_add_executor_job(
                _fetch_bridge_certificate, self._host
            )
            if self._bridge_cert and self._bridge_id:
                cn = cert_common_name(self._bridge_cert)
                if not matches_bridge_id(cn, self._bridge_id):
                    _LOGGER.error(
                        "Bridge at %s presented a certificate for '%s' but "
                        "reports bridge id '%s'",
                        self._host, cn, self._bridge_id,
                    )
                    return self.async_abort(reason="certificate_mismatch")
            try:
                ctx = await self._verified_ssl()
                self._app_key, self._client_key = await create_app_key(
                    session, self._host, ctx, devicetype=_devicetype(self.hass)
                )
            except LinkButtonNotPressed:
                errors["base"] = "link_button"
            except (aiohttp.ClientSSLError, ssl.SSLError, ssl.CertificateError) as err:
                # The certificate chains to neither Signify root nor the
                # captured pin. Refuse rather than silently downgrading: this is
                # the only failure mode an on-path attacker can force. aiohttp's
                # own TLS errors are also OSErrors, so this clause has to come
                # before the generic connection handling below.
                _LOGGER.error("Bridge TLS validation failed for %s: %s", self._host, err)
                return self.async_abort(reason="certificate_invalid")
            except (HueBridgeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                bridge = HueBridge(session, self._host, self._app_key, ctx)
                try:
                    configs = await bridge.get_entertainment_configs()
                except (HueBridgeError, OSError):
                    errors["base"] = "cannot_connect"
                else:
                    if not configs:
                        return self.async_abort(reason="no_areas")
                    self._configs = {c.id: c.name for c in configs}
                    return await self.async_step_select_areas()

        return self.async_show_form(step_id="link", errors=errors)

    async def async_step_select_areas(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"Hue Music Sync ({self._host})",
                data={
                    CONF_HOST: self._host,
                    CONF_BRIDGE_ID: self._bridge_id,
                    CONF_APP_KEY: self._app_key,
                    CONF_CLIENT_KEY: self._client_key,
                    CONF_BRIDGE_CERT: self._bridge_cert,
                    CONF_AREAS: user_input[CONF_AREAS],
                },
            )

        return self.async_show_form(
            step_id="select_areas",
            data_schema=vol.Schema(
                {vol.Required(CONF_AREAS): cv.multi_select(self._configs)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return HueMusicSyncOptionsFlow()


class HueMusicSyncOptionsFlow(OptionsFlow):
    """Allow re-selecting which entertainment areas are enabled."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        manager = getattr(entry, "runtime_data", None)
        choices = {cid: cfg.name for cid, cfg in manager.configs.items()} if manager else {}

        if user_input is not None:
            new_data = dict(entry.data)
            new_data[CONF_AREAS] = user_input[CONF_AREAS]
            self.hass.config_entries.async_update_entry(entry, data=new_data)
            new_options = dict(entry.options)
            new_options[CONF_SNAPSERVER_HOST] = user_input.get(CONF_SNAPSERVER_HOST, "").strip()
            new_options[CONF_RESTORE_LIGHTS] = user_input.get(
                CONF_RESTORE_LIGHTS, DEFAULT_RESTORE_LIGHTS
            )
            new_options[CONF_SUBSONIC_URL] = user_input.get(CONF_SUBSONIC_URL, "").strip()
            new_options[CONF_SUBSONIC_USER] = user_input.get(CONF_SUBSONIC_USER, "").strip()
            new_options[CONF_SUBSONIC_PASSWORD] = user_input.get(CONF_SUBSONIC_PASSWORD, "")
            new_options[CONF_ACTIVE_BACKEND] = user_input.get(
                CONF_ACTIVE_BACKEND, DEFAULT_BACKEND
            )
            self.hass.config_entries.async_update_entry(entry, options=new_options)
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(entry.entry_id)
            )
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AREAS, default=list(entry.data.get(CONF_AREAS, []))
                    ): cv.multi_select(choices),
                    vol.Optional(
                        CONF_SNAPSERVER_HOST,
                        default=entry.options.get(CONF_SNAPSERVER_HOST, ""),
                    ): str,
                    vol.Optional(
                        CONF_RESTORE_LIGHTS,
                        default=entry.options.get(
                            CONF_RESTORE_LIGHTS, DEFAULT_RESTORE_LIGHTS
                        ),
                    ): bool,
                    # OpenSubsonic / Navidrome library: lets the integration
                    # fetch & analyse library tracks directly when MA won't
                    # expose a tappable stream (e.g. Sendspin + OpenSubsonic).
                    vol.Optional(
                        CONF_SUBSONIC_URL,
                        default=entry.options.get(CONF_SUBSONIC_URL, ""),
                    ): str,
                    vol.Optional(
                        CONF_SUBSONIC_USER,
                        default=entry.options.get(CONF_SUBSONIC_USER, ""),
                    ): str,
                    vol.Optional(
                        CONF_SUBSONIC_PASSWORD,
                        default=entry.options.get(CONF_SUBSONIC_PASSWORD, ""),
                    ): str,
                    # Which library the Synco player browses/plays. Music
                    # Assistant is the full-feature backend (grouping, sync);
                    # Navidrome/OpenSubsonic (direct) keeps the player working
                    # when MA is down (needs the URL + login above).
                    vol.Optional(
                        CONF_ACTIVE_BACKEND,
                        default=entry.options.get(CONF_ACTIVE_BACKEND, DEFAULT_BACKEND),
                    ): vol.In(
                        {
                            BACKEND_MA: "Music Assistant",
                            BACKEND_SUBSONIC: "Navidrome / OpenSubsonic (direct)",
                        }
                    ),
                }
            ),
        )
