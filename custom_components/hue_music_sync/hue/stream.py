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
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor

from ..const import (
    HUE_DTLS_PORT,
    HUE_STREAM_PROTOCOL,
    HUE_STREAM_VERSION,
    KEEPALIVE_INTERVAL,
    MAX_CHANNELS_PER_PACKET,
)
from .dtls import (
    ALERT_CLOSE_NOTIFY,
    ALERT_NAMES,
    ALERT_USER_CANCELED,
    DtlsError,
    DtlsPeerClosed,
    DtlsPskClient,
)

_LOGGER = logging.getLogger(__name__)

ColorSpaceRGB = 0x00
ColorSpaceXYB = 0x01


class StreamRevoked(ConnectionError):
    """The bridge deliberately ended our stream; do not try to reclaim it.

    Raised when the bridge sends ``close_notify`` (documented as "streaming is
    disabled via CLIP") or ``user_canceled``. Subclasses ``ConnectionError`` so
    existing handlers still catch it, but lets the sync loop tell a revocation
    apart from a dropped packet and stop instead of re-issuing ``action=start``.
    """


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


def _gam(c: float) -> float:
    return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92


# Philips Hue Gamut C (current colour bulbs): the triangle of xy chromaticities
# the bulbs can actually reproduce. Points outside get snapped by the bridge
# unpredictably, so we clamp to the triangle ourselves for deterministic colour.
GAMUT_C = ((0.6915, 0.3038), (0.1700, 0.7000), (0.1532, 0.0475))  # red, green, blue
# Max xy chromaticity movement per emitted frame. The bridge does not interpolate
# between frames, so a big palette/album-art jump would "pop"; capping the step
# turns it into a smooth slewed move (the stream is the bulb's only smoothing).
XY_SLEW_MAX = 0.08


def _closest_on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float]:
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    t = 0.0 if denom <= 0 else ((px - ax) * abx + (py - ay) * aby) / denom
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return ax + abx * t, ay + aby * t


def clamp_to_gamut(x: float, y: float, gamut=GAMUT_C) -> tuple[float, float]:
    """Snap an xy chromaticity into the bulb's reproducible colour triangle."""
    (r, g, b) = gamut
    # Sign of the cross products tells us which side of each edge the point is on;
    # if it's inside (consistent winding) leave it, else project to the nearest edge.
    def cross(o, a, p):
        return (a[0] - o[0]) * (p[1] - o[1]) - (a[1] - o[1]) * (p[0] - o[0])

    p = (x, y)
    d1, d2, d3 = cross(r, g, p), cross(g, b, p), cross(b, r, p)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    if not (has_neg and has_pos):
        return x, y  # inside the triangle (all same sign)
    best = None
    for ax, ay, bx, by in ((*r, *g), (*g, *b), (*b, *r)):
        cx, cy = _closest_on_segment(x, y, ax, ay, bx, by)
        d = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or d < best[0]:
            best = (d, cx, cy)
    return best[1], best[2]


def rgb_to_xy(
    r: float, g: float, b: float, gamut=GAMUT_C
) -> tuple[float, float]:
    """Hue RGB -> xy chromaticity (sRGB gamma + sRGB→XYZ D65), gamut-clamped.

    Chromaticity is scale-invariant, so callers should pass a full-brightness
    colour and carry brightness separately for stable dimming. The optional
    ``gamut`` parameter (default Gamut C) allows per-light gamut triangles
    fetched from the CLIP v2 API for accurate colour clamping on mixed setups.

    The matrix is the sRGB → XYZ D65 transform on Philips' current Colour
    Conversion page (0.4124 / 0.3576 / 0.1805 …). It replaced the "Wide-RGB
    D65" matrix (0.649926 / 0.103455 / 0.197109 …) that came from the original
    2013 developer docs and has since been superseded — the two disagree most
    on saturated greens and blues, which is exactly where the old matrix read
    wrong.
    """
    r, g, b = _gam(r), _gam(g), _gam(b)
    x_ = r * 0.4124 + g * 0.3576 + b * 0.1805
    y_ = r * 0.2126 + g * 0.7152 + b * 0.0722
    z_ = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x_ + y_ + z_
    if total <= 0:
        return 0.0, 0.0
    return clamp_to_gamut(x_ / total, y_ / total, gamut)


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

    def __init__(
        self,
        config_id: str,
        colorspace: int = ColorSpaceXYB,
        channel_gamuts: dict[int, tuple[tuple[float, float], ...]] | None = None,
    ) -> None:
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
        self._prev_xy: dict[int, tuple[float, float]] = {}  # for xy slew-limiting
        # Per-channel gamut triangles resolved from the CLIP v2 API. Each entry
        # is ((rx, ry), (gx, gy), (bx, by)). Channels without a resolved gamut
        # fall back to the default GAMUT_C in rgb_to_xy.
        self._channel_gamuts = channel_gamuts or {}

    def _header(self, colorspace: int) -> bytearray:
        self._seq = (self._seq + 1) & 0xFF
        header = bytearray()
        header += HUE_STREAM_PROTOCOL
        header += HUE_STREAM_VERSION
        header.append(self._seq)
        header += b"\x00\x00"  # reserved
        header.append(colorspace)
        header.append(0x00)  # reserved
        header += self._config_id
        return header

    def build_frame(
        self, channels: Iterable[tuple[int, int, int, int]], colorspace: int | None = None
    ) -> bytes:
        """Encode one frame.

        ``channels`` yields ``(channel_id, c0, c1, c2)`` where the three colour
        values are 16-bit ints (R,G,B for RGB, or X,Y,Brightness for xy).
        """
        frame = self._header(self._colorspace if colorspace is None else colorspace)
        for channel_id, c0, c1, c2 in channels:
            frame.append(channel_id & 0xFF)
            frame += int(c0).to_bytes(2, "big")
            frame += int(c1).to_bytes(2, "big")
            frame += int(c2).to_bytes(2, "big")
        return bytes(frame)

    def build_frame_rgb(self, channels: Mapping[int, tuple[float, float, float]]) -> bytes:
        """Encode a frame from normalised 0.0-1.0 RGB tuples keyed by channel id."""
        return self.build_frame(
            (
                (cid, float_to_16(r), float_to_16(g), float_to_16(b))
                for cid, (r, g, b) in channels.items()
            ),
            ColorSpaceRGB,
        )

    def build_frame_xy(self, channels: Mapping[int, tuple[float, float, float]]) -> bytes:
        """Encode a frame as xy + brightness from normalised RGB tuples.

        Brightness is taken as the colour's value (max channel) and sent on its
        own 16-bit channel, while chromaticity is computed from the full-bright
        colour so it stays constant as brightness changes. This is how native
        Hue (and the Spotify/Samsung integrations) dim smoothly — the bridge maps
        the dedicated brightness through the bulb's own dimming curve instead of
        us shrinking RGB magnitudes into the bridge's coarse low-value range.
        """
        def encode(cid: int, r: float, g: float, b: float) -> tuple[int, int, int]:
            gamut = self._channel_gamuts.get(cid, GAMUT_C)
            bri = max(r, g, b)
            if bri <= 1e-6:
                # Black: hold the last chromaticity (falling back to the gamut's
                # centroid) and send brightness 0. Emitting xy=(0,0) here would
                # be an out-of-gamut point the bridge snaps unpredictably, and it
                # would make the next lit frame slew from that corner instead of
                # from the hue we were actually showing.
                prev = self._prev_xy.get(cid)
                if prev is None:
                    prev = (
                        sum(p[0] for p in gamut) / 3.0,
                        sum(p[1] for p in gamut) / 3.0,
                    )
                    self._prev_xy[cid] = prev
                return float_to_16(prev[0]), float_to_16(prev[1]), 0
            x, y = rgb_to_xy(r / bri, g / bri, b / bri, gamut)
            x, y = self._slew_xy(cid, x, y)
            return float_to_16(x), float_to_16(y), float_to_16(bri)

        return self.build_frame(
            ((cid, *encode(cid, r, g, b)) for cid, (r, g, b) in channels.items()),
            ColorSpaceXYB,
        )

    def _slew_xy(self, cid: int, x: float, y: float) -> tuple[float, float]:
        """Cap how far a channel's chromaticity may move in one frame.

        The bridge sends each frame straight to the bulbs without interpolating,
        so a large jump (palette switch, album-art change, rainbow step) is a
        visible pop. Limiting the per-frame step turns it into a smooth move.
        """
        prev = self._prev_xy.get(cid)
        if prev is not None:
            dx, dy = x - prev[0], y - prev[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist > XY_SLEW_MAX:
                scale = XY_SLEW_MAX / dist
                x, y = prev[0] + dx * scale, prev[1] + dy * scale
        self._prev_xy[cid] = (x, y)
        return x, y

    def build(self, channels: Mapping[int, tuple[float, float, float]]) -> bytes:
        """Encode a frame using the encoder's configured colourspace."""
        if self._colorspace == ColorSpaceXYB:
            return self.build_frame_xy(channels)
        return self.build_frame_rgb(channels)

    def build_packets(
        self, channels: Mapping[int, tuple[float, float, float]]
    ) -> list[bytes]:
        """Encode one logical frame as one or more datagrams.

        The Entertainment API caps a packet at 20 channels, so areas with more
        channels (several lamps plus gradient-strip segments) are split across
        multiple datagrams. Each datagram is a complete HueStream frame carrying
        an explicit channel id per entry, so the bridge applies each subset
        correctly. Small areas still produce exactly one packet.
        """
        items = list(channels.items())
        if len(items) <= MAX_CHANNELS_PER_PACKET:
            return [self.build(channels)]
        return [
            self.build(dict(items[i : i + MAX_CHANNELS_PER_PACKET]))
            for i in range(0, len(items), MAX_CHANNELS_PER_PACKET)
        ]


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
        psk_identity: str | None = None,
        on_revoked: "Callable[[], None] | None" = None,
    ) -> None:
        self._host = host
        self._app_key = app_key
        self._client_key = client_key  # hex string
        self._port = port
        # Per the Hue Entertainment API spec, the DTLS PSK identity must be the
        # hue-application-id (fetched from /auth/v1), NOT the app key. When not
        # available (older firmware, or fetch failed), fall back to the app key
        # as before — the bridge currently accepts both, but the spec says to
        # use the application id and future firmware may enforce it.
        self._psk_identity = psk_identity or app_key
        self._client: DtlsPskClient | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._last_frame: bytes | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        # Set once the bridge tells us the stream is over (close_notify /
        # user_canceled). Latched so every later send fails fast as a revocation
        # instead of looking like a transient network error.
        self._revoked: DtlsPeerClosed | None = None
        # Called from the keepalive loop when a revocation is detected, so the
        # session can tear down promptly rather than waiting for the next send.
        self._on_revoked = on_revoked
        # Dedicated single-thread executor for the socket work: at up to
        # ~50-100 datagrams/s, bouncing every frame off HA's SHARED default
        # executor contends with everything else in the process (file I/O,
        # other integrations). One private worker keeps sends ordered and off
        # both the event loop and the shared pool.
        self._executor: ThreadPoolExecutor | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._closed

    async def start(self) -> None:
        """Open the UDP socket and perform the DTLS handshake (in executor)."""
        try:
            psk = bytes.fromhex(self._client_key)
        except ValueError as err:
            raise ConnectionError(f"clientkey is not valid hex: {err}") from err

        client = DtlsPskClient(
            self._host, self._port, self._psk_identity.encode(), psk
        )
        loop = asyncio.get_running_loop()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="hue-dtls"
            )
        try:
            await loop.run_in_executor(self._executor, client.connect)
        except (DtlsError, OSError) as err:
            raise ConnectionError(
                f"DTLS handshake to {self._host}:{self._port} failed: {err}"
            ) from err
        except BaseException:
            # Cancelled (or crashed) while the handshake thread was running:
            # the thread may still COMPLETE the handshake unaware, leaving a
            # live session on the bridge that blocks the next connect. Close
            # the socket under it so the bridge side is torn down either way.
            await loop.run_in_executor(None, client.close)
            raise
        self._client = client
        self._closed = False
        self._revoked = None  # fresh session; a previous revocation is history
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def send(self, frame: bytes) -> None:
        """Send one HueStream datagram."""
        client = self._client
        if client is None or self._closed:
            raise ConnectionError("DTLS channel is not connected")
        if self._revoked is not None:
            raise StreamRevoked(str(self._revoked))
        async with self._write_lock:
            self._last_frame = frame
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._executor, client.send, frame)
            except DtlsPeerClosed as err:
                self._revoked = err
                raise StreamRevoked(str(err)) from err
            except (DtlsError, OSError) as err:
                raise ConnectionError(f"DTLS send failed: {err}") from err

    async def check_alert(self) -> DtlsPeerClosed | None:
        """Read any pending DTLS alert; returns the close reason if revoked.

        Cheap enough to call from the keepalive loop (once per ~9 s), which
        keeps it off the 60 Hz send path entirely while still catching a
        bridge-side teardown within one keepalive period.
        """
        client = self._client
        if client is None or self._closed:
            return None
        loop = asyncio.get_running_loop()
        try:
            alert = await loop.run_in_executor(self._executor, client.poll_alert)
        except (DtlsError, OSError):
            return None
        if alert is None:
            return None
        level, desc = alert
        if desc in (ALERT_CLOSE_NOTIFY, ALERT_USER_CANCELED):
            closed = DtlsPeerClosed(desc)
            self._revoked = closed
            return closed
        _LOGGER.debug(
            "DTLS alert from bridge: level=%d %s",
            level, ALERT_NAMES.get(desc, desc),
        )
        return None

    @property
    def revoked(self) -> DtlsPeerClosed | None:
        """The alert that ended this stream, if the bridge revoked it."""
        return self._revoked

    async def _keepalive_loop(self) -> None:
        """Resend the last frame if idle, so the bridge keeps the channel open.

        Doubles as the alert reader: the bridge announces a CLIP-side stop with
        ``close_notify``, and this is the only place that ever reads the socket.
        """
        try:
            while not self._closed and self.connected:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if not self.connected:
                    break
                if await self.check_alert() is not None:
                    if self._on_revoked is not None:
                        self._on_revoked()
                    break
                if self._last_frame is not None and self.connected:
                    try:
                        await self.send(self._last_frame)
                    except StreamRevoked:
                        if self._on_revoked is not None:
                            self._on_revoked()
                        break
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
                await loop.run_in_executor(self._executor, client.close)
            except OSError:
                pass
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False)
