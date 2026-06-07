"""Config flow: pair with the bridge and choose entertainment areas."""

from __future__ import annotations

import logging
import ssl
from typing import Any

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
    CONF_APP_KEY,
    CONF_AREAS,
    CONF_BRIDGE_ID,
    CONF_CLIENT_KEY,
    CONF_HOST,
    CONF_SNAPSERVER_HOST,
    DOMAIN,
)
from .hue.bridge import (
    HueBridge,
    HueBridgeError,
    LinkButtonNotPressed,
    create_app_key,
)

_LOGGER = logging.getLogger(__name__)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _fetch_bridge_id(session, host: str, ssl_ctx) -> str | None:
    try:
        async with session.get(f"https://{host}/api/config", ssl=ssl_ctx) as resp:
            data = await resp.json(content_type=None)
        return data.get("bridgeid")
    except Exception:  # noqa: BLE001
        return None


class HueMusicSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Hue Music Sync."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._app_key: str | None = None
        self._client_key: str | None = None
        self._bridge_id: str | None = None
        self._configs: dict[str, str] = {}
        self._ssl_ctx: ssl.SSLContext | None = None

    async def _ctx(self) -> ssl.SSLContext:
        if self._ssl_ctx is None:
            self._ssl_ctx = await self.hass.async_add_executor_job(_ssl_context)
        return self._ssl_ctx

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            session = async_get_clientsession(self.hass)
            self._bridge_id = await _fetch_bridge_id(session, self._host, await self._ctx())
            if self._bridge_id:
                await self.async_set_unique_id(self._bridge_id)
                self._abort_if_unique_id_configured()
            return await self.async_step_link()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the user to press the bridge link button, then pair."""
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            try:
                self._app_key, self._client_key = await create_app_key(
                    session, self._host, await self._ctx()
                )
            except LinkButtonNotPressed:
                errors["base"] = "link_button"
            except (HueBridgeError, OSError):
                errors["base"] = "cannot_connect"
            else:
                bridge = HueBridge(session, self._host, self._app_key, await self._ctx())
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
                }
            ),
        )
