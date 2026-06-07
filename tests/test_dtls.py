"""Tests for the pure-Python DTLS 1.2 PSK building blocks.

The live handshake can only be validated against a real bridge (see
scripts/spike_dtls.py); here we lock down the crypto and record framing that are
the easiest things to get subtly wrong.
"""

from __future__ import annotations

from hue_music_sync.hue.dtls import (
    DtlsPskClient,
    _CIPHER_PSK_AES128_GCM_SHA256,
    _DTLS_1_2,
    prf,
    psk_premaster,
)


def test_prf_matches_tls12_sha256_vector():
    # Well-known TLS 1.2 PRF-SHA256 test vector.
    secret = bytes.fromhex("9bbe436ba940f017b17652849a71db35")
    seed = bytes.fromhex("a0ba9f936cda311827a6f796ffd5198c")
    expected = bytes.fromhex(
        "e3f229ba727be17b8d122620557cd453c2aab21d07c3d495329b52d4e61edb5a"
        "6b301791e90d35c9c9a46b4e14baf9af0fa022f7077def17abfd3797c0564bab"
        "4fbc91666e9def9b97fce34f796789baa48082d122ee42c5a72e5a5110fff701"
        "87347b66"
    )
    assert prf(secret, b"test label", seed, 100) == expected


def test_psk_premaster_structure():
    # RFC 4279: uint16(N) || zeros(N) || uint16(len psk) || psk, N = len(psk).
    assert psk_premaster(b"ABCD").hex() == "000400000000000441424344"


def test_gcm_record_roundtrip():
    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    # 40-byte key block -> derive keys; force both directions to share keys so a
    # client-encrypted record can be decrypted via the server path.
    from hue_music_sync.hue.dtls import AESGCM, _Keys

    client._keys = _Keys(bytes(range(40)))
    client._keys.server_key = client._keys.client_key
    client._keys.server_iv = client._keys.client_iv
    client._gcm_client = AESGCM(client._keys.client_key)
    client._gcm_server = AESGCM(client._keys.server_key)

    seq = b"\x00\x01" + (5).to_bytes(6, "big")
    fragment = client._encrypt(23, seq, b"hello world")
    assert fragment[:8] == seq  # explicit nonce prepended
    assert client._decrypt(23, seq, fragment) == b"hello world"


def test_client_hello_body_has_cipher_and_cookie():
    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    client._client_random = b"\x11" * 32
    body = client._client_hello_body(b"COOKIE")
    assert body[:2] == _DTLS_1_2
    assert body[2:34] == b"\x11" * 32
    assert b"COOKIE" in body
    assert _CIPHER_PSK_AES128_GCM_SHA256 in body


def test_record_increments_sequence():
    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    r0 = client._record(23, b"x", encrypt=False)
    r1 = client._record(23, b"y", encrypt=False)
    # epoch(2)+seq(6) live at bytes 3..11; sequence must advance.
    assert r0[3:11] == b"\x00\x00" + (0).to_bytes(6, "big")
    assert r1[3:11] == b"\x00\x00" + (1).to_bytes(6, "big")
