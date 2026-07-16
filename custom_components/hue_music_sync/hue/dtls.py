"""Minimal pure-Python DTLS 1.2 client for the Hue Entertainment API.

The Hue bridge only accepts entertainment colour data over a DTLS 1.2 channel
secured with a pre-shared key and the single cipher suite
``TLS_PSK_WITH_AES_128_GCM_SHA256``. The usual options for DTLS in Python don't
work inside the Home Assistant container: ``python-mbedtls`` has no wheels for
current Python, the ``openssl`` CLI isn't installed in the HA image, and
``pyOpenSSL`` exposes no PSK callback. So this module implements just enough of
DTLS 1.2 — PSK key exchange, the TLS 1.2 PRF, and AES-128-GCM record protection
— on top of the stdlib and ``cryptography`` (which HA always bundles) to drive
the one cipher suite the bridge needs.

Scope is deliberately narrow: one cipher suite, no certificates, no
renegotiation, unfragmented handshake flights (the bridge's messages are small).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import socket
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_LOGGER = logging.getLogger(__name__)

# Content types
_CT_CHANGE_CIPHER_SPEC = 20
_CT_ALERT = 21
_CT_HANDSHAKE = 22
_CT_APPLICATION_DATA = 23

# Handshake types
_HS_CLIENT_HELLO = 1
_HS_SERVER_HELLO = 2
_HS_HELLO_VERIFY_REQUEST = 3
_HS_SERVER_KEY_EXCHANGE = 12
_HS_SERVER_HELLO_DONE = 14
_HS_CLIENT_KEY_EXCHANGE = 16
_HS_FINISHED = 20

_DTLS_1_2 = b"\xfe\xfd"
_CIPHER_PSK_AES128_GCM_SHA256 = b"\x00\xa8"

_HANDSHAKE_TIMEOUT = 1.0  # per-flight socket timeout
_HANDSHAKE_RETRIES = 6


class DtlsError(Exception):
    """DTLS handshake or transport failure."""


# --- TLS 1.2 PRF (P_SHA256) ----------------------------------------------

def _p_hash(secret: bytes, seed: bytes, length: int) -> bytes:
    out = bytearray()
    a = seed
    while len(out) < length:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        out += hmac.new(secret, a + seed, hashlib.sha256).digest()
    return bytes(out[:length])


def prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    """TLS 1.2 PRF using SHA-256."""
    return _p_hash(secret, label + seed, length)


def psk_premaster(psk: bytes) -> bytes:
    """Build the PSK pre-master secret (RFC 4279): zeros||psk with length prefixes."""
    other = b"\x00" * len(psk)
    return (
        struct.pack(">H", len(other)) + other + struct.pack(">H", len(psk)) + psk
    )


# --- byte helpers --------------------------------------------------------

def _u24(n: int) -> bytes:
    return n.to_bytes(3, "big")


def _vec8(data: bytes) -> bytes:
    return bytes([len(data)]) + data


def _vec16(data: bytes) -> bytes:
    return struct.pack(">H", len(data)) + data


class _Keys:
    """Derived AES-128-GCM record keys for one direction pair."""

    __slots__ = ("client_key", "server_key", "client_iv", "server_iv")

    def __init__(self, key_block: bytes) -> None:
        self.client_key = key_block[0:16]
        self.server_key = key_block[16:32]
        self.client_iv = key_block[32:36]
        self.server_iv = key_block[36:40]


class DtlsPskClient:
    """Synchronous DTLS 1.2 PSK client speaking TLS_PSK_WITH_AES_128_GCM_SHA256."""

    def __init__(self, host: str, port: int, identity: bytes, psk: bytes) -> None:
        self._addr = (host, port)
        self._identity = identity
        self._psk = psk
        self._sock: socket.socket | None = None

        self._client_random = b""
        self._server_random = b""
        self._cookie = b""
        self._transcript = bytearray()  # canonical handshake messages for Finished
        self._client_msg_seq = 0

        self._send_epoch = 0
        self._send_seq = 0  # record sequence within current send epoch
        self._keys: _Keys | None = None
        self._gcm_client: AESGCM | None = None
        self._gcm_server: AESGCM | None = None

    # -- record framing ---------------------------------------------------

    def _record(self, content_type: int, fragment: bytes, *, encrypt: bool) -> bytes:
        epoch = self._send_epoch
        seq = self._send_seq
        self._send_seq += 1
        seq_bytes = struct.pack(">H", epoch) + seq.to_bytes(6, "big")
        if encrypt:
            fragment = self._encrypt(content_type, seq_bytes, fragment)
        return (
            bytes([content_type]) + _DTLS_1_2 + seq_bytes
            + struct.pack(">H", len(fragment)) + fragment
        )

    def _encrypt(self, content_type: int, seq_bytes: bytes, plaintext: bytes) -> bytes:
        assert self._gcm_client and self._keys
        explicit = seq_bytes  # 8 bytes: epoch||seq, used as explicit nonce
        nonce = self._keys.client_iv + explicit
        aad = seq_bytes + bytes([content_type]) + _DTLS_1_2 + struct.pack(">H", len(plaintext))
        ct = self._gcm_client.encrypt(nonce, plaintext, aad)
        return explicit + ct

    def _decrypt(self, content_type: int, seq_bytes: bytes, fragment: bytes) -> bytes:
        assert self._gcm_server and self._keys
        explicit, ct = fragment[:8], fragment[8:]
        nonce = self._keys.server_iv + explicit
        plen = len(ct) - 16
        aad = seq_bytes + bytes([content_type]) + _DTLS_1_2 + struct.pack(">H", plen)
        return self._gcm_server.decrypt(nonce, ct, aad)

    def _handshake_msg(self, hs_type: int, body: bytes) -> bytes:
        """Build a (single-fragment) handshake message and advance message_seq."""
        header = (
            bytes([hs_type]) + _u24(len(body))
            + struct.pack(">H", self._client_msg_seq)
            + _u24(0) + _u24(len(body))
        )
        self._client_msg_seq += 1
        msg = header + body
        self._transcript += msg
        return msg

    # -- handshake --------------------------------------------------------

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(_HANDSHAKE_TIMEOUT)
        self._sock.connect(self._addr)
        try:
            self._do_handshake()
        except Exception:
            self.close()
            raise

    def _do_handshake(self) -> None:
        self._client_random = os.urandom(32)

        # Flight 1: ClientHello (no cookie). This message and the
        # HelloVerifyRequest are excluded from the Finished transcript, so send
        # it without recording it, then rebuild ClientHello with the cookie.
        self._send_records([self._record(_CT_HANDSHAKE, self._client_hello_body_msg(record=False), encrypt=False)])

        hvr = self._await_handshake({_HS_HELLO_VERIFY_REQUEST})
        self._cookie = self._parse_hello_verify(hvr[_HS_HELLO_VERIFY_REQUEST])

        # Flight 3: ClientHello (with cookie) — first message of the real transcript.
        self._client_msg_seq = 1
        ch = self._handshake_msg(_HS_CLIENT_HELLO, self._client_hello_body(self._cookie))
        self._send_records([self._record(_CT_HANDSHAKE, ch, encrypt=False)])

        msgs = self._await_handshake({_HS_SERVER_HELLO, _HS_SERVER_HELLO_DONE})
        self._parse_server_hello(msgs[_HS_SERVER_HELLO])
        # ServerKeyExchange (PSK identity hint) is optional; record it if present.
        # (Already captured into transcript by _await_handshake.)

        master = self._derive_master()
        key_block = prf(master, b"key expansion", self._server_random + self._client_random, 40)
        self._keys = _Keys(key_block)
        self._gcm_client = AESGCM(self._keys.client_key)
        self._gcm_server = AESGCM(self._keys.server_key)

        # Flight 5: ClientKeyExchange, ChangeCipherSpec, Finished.
        cke = self._handshake_msg(_HS_CLIENT_KEY_EXCHANGE, _vec16(self._identity))
        cke_record = self._record(_CT_HANDSHAKE, cke, encrypt=False)

        ccs_record = self._record(_CT_CHANGE_CIPHER_SPEC, b"\x01", encrypt=False)
        # After CCS, switch to epoch 1 and reset record sequence.
        self._send_epoch = 1
        self._send_seq = 0

        verify = prf(master, b"client finished", hashlib.sha256(self._transcript).digest(), 12)
        finished_msg = self._handshake_msg(_HS_FINISHED, verify)
        finished_record = self._record(_CT_HANDSHAKE, finished_msg, encrypt=True)

        self._send_records([cke_record, ccs_record, finished_record])

        # Expect server ChangeCipherSpec + encrypted Finished. Enforced: the
        # server's Finished is the only message that proves the peer actually
        # knows the PSK — without checking it we would stream to any impostor
        # that echoed the plaintext flights.
        self._await_server_finished(master)

    def _client_hello_body(self, cookie: bytes) -> bytes:
        return (
            _DTLS_1_2
            + self._client_random
            + _vec8(b"")  # session_id
            + _vec8(cookie)
            + _vec16(_CIPHER_PSK_AES128_GCM_SHA256)  # cipher_suites
            + _vec8(b"\x00")  # compression: null
        )

    def _client_hello_body_msg(self, record: bool) -> bytes:
        """ClientHello#1 as a raw handshake message (msg_seq 0, not recorded)."""
        body = self._client_hello_body(b"")
        return (
            bytes([_HS_CLIENT_HELLO]) + _u24(len(body))
            + struct.pack(">H", 0) + _u24(0) + _u24(len(body)) + body
        )

    def _parse_hello_verify(self, body: bytes) -> bytes:
        # ProtocolVersion(2) + cookie<0..255>
        cookie_len = body[2]
        return body[3 : 3 + cookie_len]

    def _parse_server_hello(self, body: bytes) -> None:
        off = 2  # skip server_version
        self._server_random = body[off : off + 32]
        off += 32
        sid_len = body[off]
        off += 1 + sid_len
        cipher = body[off : off + 2]
        if cipher != _CIPHER_PSK_AES128_GCM_SHA256:
            raise DtlsError(f"bridge selected unexpected cipher {cipher.hex()}")

    def _derive_master(self) -> bytes:
        pms = psk_premaster(self._psk)
        return prf(pms, b"master secret", self._client_random + self._server_random, 48)

    # -- receive ----------------------------------------------------------

    def _await_handshake(self, needed: set[int]) -> dict[int, bytes]:
        """Read records until all needed handshake types are seen.

        Server handshake messages (ServerHello, optional ServerKeyExchange,
        ServerHelloDone) are appended to the transcript in arrival order.
        """
        collected: dict[int, bytes] = {}
        for _ in range(_HANDSHAKE_RETRIES):
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                self._retransmit_last_flight()
                continue
            for ctype, seq_bytes, fragment in self._split_records(data):
                if ctype != _CT_HANDSHAKE:
                    continue
                for hs_type, raw_msg, body in self._split_handshake(fragment):
                    if hs_type == _HS_HELLO_VERIFY_REQUEST:
                        collected[hs_type] = body
                    else:
                        # Part of the real transcript.
                        self._transcript += raw_msg
                        collected[hs_type] = body
            if needed.issubset(collected.keys()):
                return collected
        raise DtlsError(f"handshake timed out waiting for {needed - collected.keys()}")

    def _await_server_finished(self, master: bytes) -> None:
        """Require and verify the server's Finished (mutual PSK confirmation).

        Epoch-1 records are detected from the record header (not from having
        seen the CCS in the same datagram — UDP may split or drop it), our
        final flight is retransmitted on each timeout per DTLS retransmission
        rules, and a missing or mismatching Finished fails the handshake.
        A successful AES-GCM decrypt already proves the peer derived the same
        keys; the verify_data check additionally binds the whole transcript.
        """
        for _ in range(_HANDSHAKE_RETRIES):
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                self._retransmit_last_flight()
                continue
            for ctype, seq_bytes, fragment in self._split_records(data):
                epoch = struct.unpack(">H", seq_bytes[:2])[0]
                if ctype != _CT_HANDSHAKE or epoch != 1:
                    continue  # CCS, retransmits of epoch-0 flights, etc.
                try:
                    plain = self._decrypt(ctype, seq_bytes, fragment)
                except Exception as err:
                    raise DtlsError(
                        "server Finished failed to decrypt (wrong PSK on one side?)"
                    ) from err
                # plain is a Finished handshake message; verify_data follows header.
                if plain and plain[0] == _HS_FINISHED:
                    verify = plain[12:24]
                    expected = prf(
                        master, b"server finished",
                        hashlib.sha256(self._transcript).digest(), 12,
                    )
                    if not hmac.compare_digest(verify, expected):
                        raise DtlsError("server Finished verification failed")
                    return
        raise DtlsError(
            "no server Finished received; peer never proved knowledge of the PSK"
        )

    def _split_records(self, data: bytes):
        off = 0
        while off + 13 <= len(data):
            ctype = data[off]
            seq_bytes = data[off + 3 : off + 11]  # epoch(2)+seq(6)
            length = struct.unpack(">H", data[off + 11 : off + 13])[0]
            fragment = data[off + 13 : off + 13 + length]
            off += 13 + length
            yield ctype, seq_bytes, fragment

    def _split_handshake(self, fragment: bytes):
        off = 0
        while off + 12 <= len(fragment):
            hs_type = fragment[off]
            length = int.from_bytes(fragment[off + 1 : off + 4], "big")
            frag_off = int.from_bytes(fragment[off + 6 : off + 9], "big")
            frag_len = int.from_bytes(fragment[off + 9 : off + 12], "big")
            body = fragment[off + 12 : off + 12 + frag_len]
            # Canonical single-fragment form for the transcript.
            raw = (
                fragment[off : off + 6]  # type + length + message_seq
                + _u24(0) + _u24(length) + body
            )
            off += 12 + frag_len
            if frag_off != 0 or frag_len != length:
                _LOGGER.debug("Fragmented handshake message not fully supported")
            yield hs_type, raw, body

    # -- flight bookkeeping ----------------------------------------------

    def _send_records(self, records: list[bytes]) -> None:
        self._last_flight = records
        for rec in records:
            self._sock.sendall(rec)

    def _retransmit_last_flight(self) -> None:
        for rec in getattr(self, "_last_flight", []):
            try:
                self._sock.sendall(rec)
            except OSError:
                break

    # -- application data -------------------------------------------------

    def send(self, data: bytes) -> None:
        if self._sock is None or self._keys is None:
            raise DtlsError("DTLS channel not connected")
        self._sock.sendall(self._record(_CT_APPLICATION_DATA, data, encrypt=True))

    def close(self) -> None:
        if self._sock is not None:
            # Send a close_notify alert first (best-effort): without it the
            # bridge keeps the DTLS session (and the entertainment stream)
            # alive until its ~10 s idle timeout, and a new handshake started
            # in that window is silently ignored — the "turn sync back on
            # right after turning it off" failure.
            if self._keys is not None:
                try:
                    alert = bytes([1, 0])  # AlertLevel.warning, close_notify
                    self._sock.sendall(self._record(_CT_ALERT, alert, encrypt=True))
                except Exception:  # noqa: BLE001 - best-effort
                    pass
            try:
                self._sock.close()
            finally:
                self._sock = None
