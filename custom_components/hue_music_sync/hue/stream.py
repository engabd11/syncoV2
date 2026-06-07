"""Hue Entertainment DTLS streaming transport + HueStream v2 frame encoder.

The bridge accepts colour data only over a DTLS 1.2 channel secured with a
pre-shared key (PSK = the bridge ``clientkey``, identity = the application key)
and ``TLS_PSK_WITH_AES_128_GCM_SHA256``. None of the off-the-shelf Python DTLS
options work inside the Home Assistant container (no ``python-mbedtls`` wheels,
no ``openssl`` CLI, no PSK support in ``pyOpenSSL``), so the transport uses the
self-contained :class:`~.dtls.DtlsPskClient` (built on the bundled
``cryptography``). The synchronous client is driven from the event loop via the
executor.
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
from .dtls import DtlsError, DtlsPskClient

_LOGGER = logging.getLogger(__name__)

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
    """Async DTLS streaming channel backed by the pure-Python PSK client.

    The :class:`~.dtls.DtlsPskClient` is synchronous (blocking UDP socket); its
    handshake and per-frame sends are dispatched to the default executor so the
    event loop is never blocked.
    """

    def __init__(
        self,
        host: str,
        app_key: str,
        client_key: str,
        port: int = HUE_DTLS_PORT,
    ) -> None:
        self._host = host
        self._app_key = app_key
        self._client_key = client_key  # hex string
        self._port = port
        self._client: DtlsPskClient | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._last_frame: bytes | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._closed

    async def start(self) -> None:
        """Open the UDP socket and perform the DTLS handshake (in executor)."""
        try:
            psk = bytes.fromhex(self._client_key)
        except ValueError as err:
            raise ConnectionError(f"clientkey is not valid hex: {err}") from err

        client = DtlsPskClient(self._host, self._port, self._app_key.encode(), psk)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, client.connect)
        except (DtlsError, OSError) as err:
            raise ConnectionError(
                f"DTLS handshake to {self._host}:{self._port} failed: {err}"
            ) from err
        self._client = client
        self._closed = False
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def send(self, frame: bytes) -> None:
        """Send one HueStream datagram."""
        client = self._client
        if client is None or self._closed:
            raise ConnectionError("DTLS channel is not connected")
        async with self._write_lock:
            self._last_frame = frame
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, client.send, frame)
            except (DtlsError, OSError) as err:
                raise ConnectionError(f"DTLS send failed: {err}") from err

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
        client = self._client
        self._client = None
        if client is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, client.close)
            except OSError:
                pass
