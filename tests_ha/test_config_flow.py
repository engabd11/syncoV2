"""Config-flow tests: pairing happy path, discovery, certificate checks."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
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

AREA = EntertainmentConfig(id="area-1", name="Living Room", status="inactive")


def _self_signed(common_name: str) -> str:
    """A PEM whose subject CN is ``common_name``.

    The flow really parses the certificate and compares its CN to the bridge id
    (the documented stand-in for hostname verification, since the certificate
    carries no IP), so a placeholder string would not exercise the check.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


BRIDGE_CERT = _self_signed(BRIDGE_ID)
OTHER_CERT = _self_signed("0017deadbeef9999")


def _patches(configs=None, pairing=None, cert=BRIDGE_CERT, discovered=None):
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
            return_value=cert,
        ),
        patch(
            "custom_components.hue_music_sync.hue.bridge.HueBridge."
            "get_entertainment_configs",
            AsyncMock(return_value=configs if configs is not None else [AREA]),
        ),
        patch(
            "custom_components.hue_music_sync.config_flow._discover_bridges",
            AsyncMock(return_value=discovered or {}),
        ),
    )


async def test_full_flow_creates_entry(hass: HomeAssistant) -> None:
    p1, p2, p3, p4, p5 = _patches()
    with p1, p2, p3, p4, p5:
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
    assert data[CONF_BRIDGE_CERT] == BRIDGE_CERT  # pinned at pairing time
    assert data[CONF_AREAS] == ["area-1"]


async def test_link_button_not_pressed_shows_error(hass: HomeAssistant) -> None:
    p1, p2, p3, p4, p5 = _patches(pairing=LinkButtonNotPressed)
    with p1, p2, p3, p4, p5:
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
    p1, p2, p3, p4, p5 = _patches()
    with p1, p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_areas_aborts(hass: HomeAssistant) -> None:
    p1, p2, p3, p4, p5 = _patches(configs=[])
    with p1, p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_areas"


async def test_zeroconf_discovery_offers_pairing(hass: HomeAssistant) -> None:
    # mDNS is the discovery method the Bridge Discovery guide prescribes first.
    # The bridge id arrives in the service's TXT record.
    p1, p2, p3, p4, p5 = _patches()
    info = ZeroconfServiceInfo(
        ip_address=HOST,
        ip_addresses=[HOST],
        hostname="Philips-hue.local.",
        name="Philips Hue - deadbe._hue._tcp.local.",
        port=443,
        type="_hue._tcp.local.",
        properties={"bridgeid": BRIDGE_ID, "modelid": "BSB002"},
    )
    with p1, p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "link"  # straight to pairing, no IP to type

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREAS: ["area-1"]}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_BRIDGE_ID] == BRIDGE_ID


async def test_zeroconf_updates_the_host_of_a_known_bridge(hass: HomeAssistant) -> None:
    # A bridge that changed IP must update its entry, not create a second one.
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=BRIDGE_ID, data={CONF_HOST: "192.0.2.99"}
    )
    entry.add_to_hass(hass)
    info = ZeroconfServiceInfo(
        ip_address=HOST,
        ip_addresses=[HOST],
        hostname="Philips-hue.local.",
        name="Philips Hue - deadbe._hue._tcp.local.",
        port=443,
        type="_hue._tcp.local.",
        properties={"bridgeid": BRIDGE_ID},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST


async def test_the_two_discovery_paths_agree_on_the_unique_id(
    hass: HomeAssistant,
) -> None:
    # The same bridge reports its id in different cases depending on where the id
    # came from: mDNS TXT records are lower-cased, /api/config upper-cases. If the
    # flow carried that through, discovering an already-configured bridge would
    # create a *second* entry for the same hardware, so both paths canonicalise.
    info = ZeroconfServiceInfo(
        ip_address=HOST,
        ip_addresses=[HOST],
        hostname="Philips-hue.local.",
        name="Philips Hue - deadbe._hue._tcp.local.",
        port=443,
        type="_hue._tcp.local.",
        properties={"bridgeid": BRIDGE_ID.lower(), "modelid": "BSB002"},
    )
    # Manual step first, with the endpoint shouting the id back in upper case.
    p1, p2, p3, p4, p5 = _patches()
    with patch(
        "custom_components.hue_music_sync.config_flow._fetch_bridge_id",
        AsyncMock(return_value=BRIDGE_ID.upper()),
    ), p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_AREAS: ["area-1"]}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_BRIDGE_ID] == BRIDGE_ID

    # The same bridge then announcing itself over mDNS must update that entry.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_a_legacy_upper_cased_entry_is_not_duplicated(
    hass: HomeAssistant,
) -> None:
    # Entries created before the ids were canonicalised keep their spelling, so
    # an upgrade must recognise them rather than pair the bridge a second time.
    MockConfigEntry(
        domain=DOMAIN, unique_id=BRIDGE_ID.upper(), data={CONF_HOST: "192.0.2.99"}
    ).add_to_hass(hass)
    info = ZeroconfServiceInfo(
        ip_address=HOST,
        ip_addresses=[HOST],
        hostname="Philips-hue.local.",
        name="Philips Hue - deadbe._hue._tcp.local.",
        port=443,
        type="_hue._tcp.local.",
        properties={"bridgeid": BRIDGE_ID},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].unique_id == BRIDGE_ID.upper()  # spelling preserved
    assert entries[0].data[CONF_HOST] == HOST  # and the IP still updated


async def test_zeroconf_without_a_bridge_id_is_ignored(hass: HomeAssistant) -> None:
    info = ZeroconfServiceInfo(
        ip_address=HOST,
        ip_addresses=[HOST],
        hostname="something.local.",
        name="something._hue._tcp.local.",
        port=443,
        type="_hue._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_hue_bridge"


async def test_certificate_for_a_different_bridge_aborts_before_pairing(
    hass: HomeAssistant,
) -> None:
    # The clientkey is minted once and never retrievable again, so a certificate
    # that doesn't belong to this bridge must stop the flow *before* the pairing
    # POST — not after it has already been handed over.
    p1, p2, p3, p4, p5 = _patches(cert=OTHER_CERT)
    with p1, p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "certificate_mismatch"


async def test_unreachable_bridge_shows_an_error(hass: HomeAssistant) -> None:
    p1, p2, p3, p4, p5 = _patches()
    with patch(
        "custom_components.hue_music_sync.config_flow._fetch_bridge_id",
        AsyncMock(return_value=None),
    ), p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: HOST}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_cloud_discovery_prefills_the_host(hass: HomeAssistant) -> None:
    # discovery.meethue.com is the documented fallback when mDNS finds nothing.
    p1, p2, p3, p4, p5 = _patches(discovered={BRIDGE_ID: HOST})
    with p1, p2, p3, p4, p5:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    (host_key,) = [k for k in schema if str(k) == CONF_HOST]
    assert host_key.default() == HOST
