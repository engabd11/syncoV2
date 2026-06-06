"""Hue Entertainment DTLS streaming transport + HueStream v2 frame encoder.

The bridge accepts colour data only over a DTLS 1.2 channel secured with a
pre-shared key (PSK = the bridge ``clientkey``, identity = the application key)
and the ``PSK-AES128-GCM-SHA256`` cipher. ``python-mbedtls`` — the usual Python
DTLS option — has no wheels for Python 3.13 (what current HAOS runs), so the
default transport shells out to the ``openssl`` CLI that ships in HA's Alpine
container. ``-quiet`` is important: it turns on ``ign_eof`` inside ``s_client``,
which disables the interactive command-character handling (``Q``/``R`` at the
start of a line) so arbitrary binary frames can be streamed safely.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping

from ..const import (
    HUE_DTLS_PORT,
    HUE_STREAM_PROTOCOL,
    HUE_STREAM_VERSION,
    KEEPALIVE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# OpenSSL cipher name for TLS_PSK_WITH_AES_128_GCM_SHA256.
_CIPHER = "PSK-AES128-GCM-SHA256"

ColorSpaceRGB = 0x00
ColorSpaceXYB = 0x01


def rgb8_to_16(value: int) -> int:
    """Scale an 8-bit channel value (0-255) to 16-bit (0-65535)."""
    value = 0 if value < 0 else 255 if value > 255 else value
    return value * 257  # 255*257 == 65535, exact endpoints


def float_to_16(value: float) -> int:
    """Scale a normalised 0.0-1.0 value to a 16-bit channel value."""
    if value <= 0.0:
        return 0
    if value >= 1.0:
        return 65535
    return int(value * 65535 + 0.5)


class HueStreamEncoder:
    """Builds HueStream v2 datagrams for an entertainment configuration.

    The frame layout (confirmed against the Hue Entertainment v2 spec):

        b"HueStream"            9 bytes, protocol name
        0x02 0x00               streaming API version 2.0
        <seq>                   1 byte sequence id (informational)
        0x00 0x00               reserved
        <colorspace>            0x00 RGB | 0x01 xy+brightness
        0x00                    reserved
        <config uuid>           36 ASCII bytes
        then per channel:       <channel id> + 3x uint16 big-endian
    """

    def __init__(self, config_id: str, colorspace: int = ColorSpaceRGB) -> None:
        raw = config_id.encode("ascii", "ignore")
        if len(raw) != 36:
            # The HueStream v2 id field is a fixed 36 bytes; pad/truncate rather
            # than crash setup if a bridge returns an unexpected id length.
            _LOGGER.warning(
                "Entertainment config id %r is %d bytes, expected 36; adjusting",
                config_id, len(raw),
            )
            raw = raw[:36].ljust(36, b"\x00")
        self._config_id = raw
        self._colorspace = colorspace
        self._seq = 0

    def _header(self) -> bytearray:
        self._seq = (self._seq + 1) & 0xFF
        header = bytearray()
        header += HUE_STREAM_PROTOCOL
        header += HUE_STREAM_VERSION
        header.append(self._seq)
        header += b"\x00\x00"  # reserved
        header.append(self._colorspace)
        header.append(0x00)  # reserved
        header += self._config_id
        return header

    def build_frame(self, channels: Iterable[tuple[int, int, int, int]]) -> bytes:
        """Encode one frame.

        ``channels`` yields ``(channel_id, c0, c1, c2)`` where the three colour
        values are 16-bit ints (R,G,B for RGB colourspace).
        """
        frame = self._header()
        for channel_id, c0, c1, c2 in channels:
            frame.append(channel_id & 0xFF)
            frame += int(c0).to_bytes(2, "big")
            frame += int(c1).to_bytes(2, "big")
            frame += int(c2).to_bytes(2, "big")
        return bytes(frame)

    def build_frame_rgb(self, channels: Mapping[int, tuple[float, float, float]]) -> bytes:
        """Encode a frame from normalised 0.0-1.0 RGB tuples keyed by channel id."""
        return self.build_frame(
            (cid, float_to_16(r), float_to_16(g), float_to_16(b))
            for cid, (r, g, b) in channels.items()
        )


class DtlsStream:
    """Async DTLS streaming channel to the bridge via the ``openssl`` CLI."""

    def __init__(
        self,
        host: str,
        app_key: str,
        client_key: str,
        openssl_bin: str = "openssl",
        port: int = HUE_DTLS_PORT,
    ) -> None:
        self._host = host
        self._app_key = app_key
        self._client_key = client_key  # hex string
        self._openssl = openssl_bin
        self._port = port
        self._proc: asyncio.subprocess.Process | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._last_frame: bytes | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, *, handshake_timeout: float = 8.0) -> None:
        """Spawn openssl and wait for the DTLS handshake to settle."""
        args = [
            self._openssl,
            "s_client",
            "-dtls1_2",
            "-cipher",
            _CIPHER,
            "-psk_identity",
            self._app_key,
            "-psk",
            self._client_key,
            "-connect",
            f"{self._host}:{self._port}",
            "-quiet",
        ]
        _LOGGER.debug("Starting DTLS via: %s s_client ... %s:%s", self._openssl, self._host, self._port)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as err:
            raise ConnectionError(
                f"'{self._openssl}' not found; the openssl CLI is required for "
                "Hue Entertainment DTLS streaming"
            ) from err

        # openssl is quiet on success; a fast exit means the handshake failed.
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass  # still running -> handshake presumed up
        else:
            err = b""
            if self._proc.stderr is not None:
                err = await self._proc.stderr.read()
            raise ConnectionError(
                f"openssl DTLS handshake to {self._host}:{self._port} failed "
                f"(exit {self._proc.returncode}): {err.decode(errors='replace').strip()}"
            )

        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def send(self, frame: bytes) -> None:
        """Send one HueStream datagram."""
        if not self.connected or self._proc is None or self._proc.stdin is None:
            raise ConnectionError("DTLS channel is not connected")
        async with self._write_lock:
            self._last_frame = frame
            self._proc.stdin.write(frame)
            await self._proc.stdin.drain()

    async def _keepalive_loop(self) -> None:
        """Resend the last frame if idle, so the bridge keeps the channel open."""
        try:
            while not self._closed and self.connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._last_frame is not None and self.connected:
                    try:
                        await self.send(self._last_frame)
                    except ConnectionError:
                        break
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Tear down the DTLS channel."""
        self._closed = True
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.returncode is None:
                if proc.stdin is not None:
                    proc.stdin.close()
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except ProcessLookupError:
            pass
