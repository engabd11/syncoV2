"""Tests for Hue bridge certificate validation helpers.

The "Using HTTPS" guide asks for two things: chain validation against Signify's
root CAs, and a check that the certificate's subject CN is the bridge id (the
certificate never carries the bridge's IP, so ordinary hostname verification
cannot substitute).
"""

from __future__ import annotations

import ssl

import pytest

from hue_music_sync.hue.certs import (
    HUE_ROOT_CA_ACTIVE,
    HUE_ROOT_CA_BUNDLE,
    HUE_ROOT_CA_SECONDARY,
    cert_common_name,
    matches_bridge_id,
)


def test_bundle_holds_both_documented_roots():
    assert HUE_ROOT_CA_BUNDLE.count("BEGIN CERTIFICATE") == 2
    assert HUE_ROOT_CA_ACTIVE in HUE_ROOT_CA_BUNDLE
    assert HUE_ROOT_CA_SECONDARY in HUE_ROOT_CA_BUNDLE


def test_bundle_loads_into_an_ssl_context():
    # If either PEM were corrupted in transcription, every bridge connection
    # would fail at setup rather than here.
    ctx = ssl.create_default_context(cadata=HUE_ROOT_CA_BUNDLE)
    assert len(ctx.get_ca_certs()) == 2


def test_common_names_of_the_roots():
    assert cert_common_name(HUE_ROOT_CA_ACTIVE) == "root-bridge"
    assert cert_common_name(HUE_ROOT_CA_SECONDARY) == "Hue Root CA 01"


def test_common_name_of_garbage_is_none():
    assert cert_common_name("not a certificate") is None
    assert cert_common_name("") is None


@pytest.mark.parametrize(
    ("cn", "bridge_id", "expected"),
    [
        # Bridge ids come back upper-cased from /api/config and lower-cased from
        # mDNS TXT records, so the comparison has to be case-insensitive.
        ("001788fffe25b8f8", "001788FFFE25B8F8", True),
        ("001788FFFE25B8F8", "001788fffe25b8f8", True),
        (" 001788fffe25b8f8 ", "001788fffe25b8f8", True),
        ("001788fffe25b8f8", "001788fffe0a1b2c", False),
        (None, "001788fffe25b8f8", False),
        ("001788fffe25b8f8", None, False),
        ("", "", False),
    ],
)
def test_bridge_id_matching(cn, bridge_id, expected):
    assert matches_bridge_id(cn, bridge_id) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("001788fffe25b8f8", True),
        ("001788FFFE25B8F8", True),
        (" 001788fffe25b8f8 ", True),
        ("001788fffe25b8f", False),   # 15 chars
        ("001788fffe25b8f88", False),  # 17 chars
        ("Philips Hue", False),
        ("zzzz88fffe25b8f8", False),   # right length, not hex
        (None, False),
        ("", False),
    ],
)
def test_bridge_id_shape_detection(value, expected):
    # Distinguishes "this certificate belongs to another bridge" (refuse) from
    # "this certificate has a subject we don't recognise" (warn, and let chain
    # validation decide).
    from hue_music_sync.hue.certs import looks_like_bridge_id

    assert looks_like_bridge_id(value) is expected
