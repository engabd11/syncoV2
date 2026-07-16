"""Config-flow tests: pairing happy path, link-button error, duplicate abort."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hue_music_sync.const import (
    CONF_APP_KEY,
    CONF_AREAS,
    CONF_BRIDGE_CERT,
    CONF_BRIDGE_ID,
    CONF_CLIENT_KEY,
    CONF_HOST,
    DOMAIN,
)
from custom_components.hue_music_sync.hue.bridge import (
    EntertainmentConfig,
    LinkButtonNotPressed,
)

BRIDGE_ID = "0017deadbeef0000"
HOST = "192.0.2.10"
FAKE_CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"

AREA = EntertainmentConfig(id="area-1", name="Living Room", status="inactive")


def _patches(configs=None, pairing=None):
    """The external-IO seams of the flow, all mocked."""
    return (
        patch(
            "custom_components.hue_music_sync.config_flow._fetch_bridge_id",
            AsyncMock(return_value=BRIDGE_ID),
        ),
        patch(
            "custom_components.hue_music_sync.config_flow.create_app_key",
            AsyncMock(side_effect=pairing) if pairing
            else AsyncMock(return_value=("app-key", "aa" * 16)),
        ),
        # config_flow imports this lazily from the package at call time.
        patch(
            "custom_components.hue_music_sync._fetch_bridge_certificate",
            return_value=FAKE_CERT,
        ),
        patch(
            "custom_components.hue_music_sync.hue.bridge.HueBridge."
            "get_entertainment_configs",
            AsyncMock(return_value=configs if configs is not None else [AREA]),
        ),
    )


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "link"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "select_areas"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREAS: ["area-1"]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_HOST] == HOST
    assert data[CONF_BRIDGE_ID] == BRIDGE_ID
    assert data[CONF_APP_KEY] == "app-key"
    assert data[CONF_CLIENT_KEY] == "aa" * 16
    assert data[CONF_BRIDGE_CERT] == FAKE_CERT  # pinned at pairing time
    assert data[CONF_AREAS] == ["area-1"]


async def test_link_button_not_pressed_shows_error(hass: HomeAssistant) -> None:
    p1, p2, p3, p4 = _patches(pairing=LinkButtonNotPressed)
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "link"
    assert result["errors"] == {"base": "link_button"}


async def test_duplicate_bridge_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id=BRIDGE_ID, data={CONF_HOST: HOST}
    ).add_to_hass(hass)
    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_areas_aborts(hass: HomeAssistant) -> None:
    p1, p2, p3, p4 = _patches(configs=[])
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_areas"
