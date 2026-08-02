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


def test_close_sends_close_notify_when_connected():
    # A graceful close must tell the bridge the session is over: without the
    # close_notify alert the bridge holds the DTLS session ~10 s and silently
    # ignores a new handshake in that window (the "turn sync back on right
    # after turning it off" failure).
    from hue_music_sync.hue.dtls import _CT_ALERT, AESGCM, _Keys

    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    client._keys = _Keys(bytes(range(40)))
    client._gcm_client = AESGCM(client._keys.client_key)
    client._send_epoch = 1

    sent: list[bytes] = []

    class _Sock:
        def sendall(self, data):
            sent.append(bytes(data))
        def close(self):
            pass

    client._sock = _Sock()
    client.close()
    assert client._sock is None
    assert len(sent) == 1
    assert sent[0][0] == _CT_ALERT  # record content type 21


def test_close_without_keys_sends_nothing():
    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    sent: list[bytes] = []

    class _Sock:
        def sendall(self, data):
            sent.append(bytes(data))
        def close(self):
            pass

    client._sock = _Sock()
    client.close()  # handshake never completed: just close the socket
    assert client._sock is None
    assert sent == []


def _alert_client() -> DtlsPskClient:
    """A client with symmetric keys, so we can forge records the server would send."""
    from hue_music_sync.hue.dtls import AESGCM, _Keys

    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    client._keys = _Keys(bytes(range(40)))
    client._keys.server_key = client._keys.client_key
    client._keys.server_iv = client._keys.client_iv
    client._gcm_client = AESGCM(client._keys.client_key)
    client._gcm_server = AESGCM(client._keys.server_key)
    return client


class _RecvSock:
    """Socket stub that hands back queued datagrams, then blocks."""

    def __init__(self, datagrams):
        self._queue = list(datagrams)
        self.timeouts = []

    def gettimeout(self):
        return 1.0

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _size):
        if self._queue:
            return self._queue.pop(0)
        raise BlockingIOError

    def sendall(self, data):
        pass

    def close(self):
        pass


def test_poll_alert_reads_encrypted_close_notify():
    # The bridge announces a CLIP-side stop ("streaming is disabled via CLIP")
    # with close_notify. Reading it is what lets the sync loop tell a deliberate
    # stop apart from a dropped packet instead of grabbing the area back.
    from hue_music_sync.hue.dtls import ALERT_CLOSE_NOTIFY, _CT_ALERT

    client = _alert_client()
    client._send_epoch = 1
    record = client._record(_CT_ALERT, bytes([2, ALERT_CLOSE_NOTIFY]), encrypt=True)
    client._sock = _RecvSock([record])

    assert client.poll_alert() == (2, ALERT_CLOSE_NOTIFY)


def test_poll_alert_restores_socket_timeout():
    # Draining must not leave the socket non-blocking: the same socket carries
    # every subsequent frame send.
    client = _alert_client()
    sock = _RecvSock([])
    client._sock = sock

    assert client.poll_alert() is None
    assert sock.timeouts == [0, 1.0]


def test_poll_alert_ignores_application_data():
    from hue_music_sync.hue.dtls import _CT_APPLICATION_DATA

    client = _alert_client()
    client._send_epoch = 1
    record = client._record(_CT_APPLICATION_DATA, b"frame", encrypt=True)
    client._sock = _RecvSock([record])

    assert client.poll_alert() is None


def test_poll_alert_ignores_undecryptable_record():
    # A stale retransmit or a record from another session must be dropped
    # quietly rather than raising into the keepalive loop.
    from hue_music_sync.hue.dtls import _CT_ALERT

    client = _alert_client()
    client._send_epoch = 1
    record = bytearray(client._record(_CT_ALERT, bytes([2, 0]), encrypt=True))
    record[-1] ^= 0xFF  # corrupt the GCM tag
    client._sock = _RecvSock([bytes(record)])

    assert client.poll_alert() is None


def test_poll_alert_needs_a_live_session():
    client = DtlsPskClient("h", 1, b"id", b"\x00" * 16)
    assert client.poll_alert() is None  # no socket, no keys


def test_peer_closed_names_the_alert():
    from hue_music_sync.hue.dtls import DtlsPeerClosed

    assert "close_notify" in str(DtlsPeerClosed(0))
    assert "user_canceled" in str(DtlsPeerClosed(90))
