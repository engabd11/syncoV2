"""Passive Sendspin metadata client: an exact playhead instead of a guessed one.

Music Assistant serves the Sendspin protocol on ``ws://<ma-host>:8927/sendspin``.
A client that declares only ``metadata@v1`` receives track metadata and the
``progress`` anchor, and — per the protocol — never becomes an audio output
target: it does not appear as a speaker and is never sent audio.

Why this matters for light timing. Home Assistant offers ``media_position`` plus
the wall time it was last written, refreshed only on seek / play-pause / track
change; everything in between has to be modelled. Sendspin instead carries:

* a microsecond server clock, synchronised by an NTP-style exchange whose offset
  *and drift* are tracked (see :class:`~.clock.ClockFilter`), and
* ``metadata.progress`` — ``track_progress`` and ``playback_speed``, stamped with
  the server time at which they are valid.

So the playhead becomes ``track_progress + (server_now - timestamp) x speed``:
exact, with an explicit rate, and re-stated by the server on every playback
change. Seeks arrive as ``stream/clear`` rather than being inferred from a
position jump a second later, and track changes arrive with the timestamp they
actually happened at. Sendspin players also schedule output *at* server
timestamps and subtract their own ``static_delay_ms``, so this is the genuinely
*audible* position — the quantity ``media_position`` can only approximate.

This is strictly an upgrade path. Everything degrades to the Home Assistant
state feed if the socket is down, MA refuses the connection, or the metadata
turns out to belong to a different group — the show never depends on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp

from .clock import ClockFilter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .clock import PlaybackClock

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8927
DEFAULT_PATH = "/sendspin"

# Clock exchanges: fast while the filter converges, slow once it has. The drift
# term is what holds accuracy in between, so steady-state chatter can be tiny.
_TIME_FAST_S = 0.5
_TIME_FAST_N = 10
_TIME_SLOW_S = 5.0

# Reconnect backoff, mirroring the DTLS/eventstream pattern used elsewhere.
_RECONNECT_BASE_S = 2.0
_RECONNECT_MAX_S = 60.0

_WS_HEARTBEAT_S = 30.0
_CONNECT_TIMEOUT_S = 10.0

STATUS_OFF = "off"
STATUS_CONNECTING = "connecting"
STATUS_SYNCING = "syncing"
STATUS_SYNCED = "synced"
STATUS_MISMATCH = "mismatch"
STATUS_ERROR = "error"


def server_url(base_url: str | None, host: str | None = None) -> str | None:
    """Build the Sendspin URL from an explicit host or Music Assistant's base URL.

    The Sendspin server lives on the same machine as Music Assistant but on its
    own port, so the MA base URL supplies the host and nothing else.
    """
    if host:
        host = host.strip()
        if host.startswith(("ws://", "wss://")):
            return host
        if "://" in host:
            host = urlsplit(host).hostname or host
        if ":" in host and not host.startswith("["):
            return f"ws://{host}{DEFAULT_PATH}"
        return f"ws://{host}:{DEFAULT_PORT}{DEFAULT_PATH}"
    if not base_url:
        return None
    parsed = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
    name = parsed.hostname
    # urlsplit happily accepts free text as a hostname, so reject anything that
    # could not be one rather than opening a socket to nonsense.
    if not name or any(c.isspace() for c in name):
        return None
    return f"ws://{name}:{DEFAULT_PORT}{DEFAULT_PATH}"


@dataclass(slots=True)
class Progress:
    """One decoded ``server/state`` progress anchor."""

    position_s: float
    server_us: float
    rate: float
    title: str | None
    artist: str | None
    duration_s: float


def parse_progress(payload: dict[str, Any]) -> Progress | None:
    """Decode a ``server/state`` payload, or None when it carries no anchor.

    Kept separate from the socket so the wire format is unit-testable against
    recorded fixtures (see ``scripts/spike_sendspin.py --record``).
    """
    meta = payload.get("metadata")
    if not isinstance(meta, dict):
        return None
    prog = meta.get("progress")
    stamp = meta.get("timestamp")
    if not isinstance(prog, dict) or stamp is None:
        return None
    try:
        pos_ms = float(prog.get("track_progress", 0.0))
        dur_ms = float(prog.get("track_duration", 0.0) or 0.0)
        # playback_speed is a x1000 multiplier; 0 means paused.
        speed = float(prog.get("playback_speed", 1000)) / 1000.0
        server_us = float(stamp)
    except (TypeError, ValueError):
        return None
    return Progress(
        position_s=pos_ms / 1000.0,
        server_us=server_us,
        rate=speed,
        title=meta.get("title"),
        artist=meta.get("artist"),
        duration_s=dur_ms / 1000.0,
    )


def _same_track(a: str | None, b: str | None) -> bool:
    """Loose title comparison — providers differ on case and padding."""
    if a is None or b is None:
        return True  # nothing to contradict
    return a.strip().casefold() == b.strip().casefold()


class SendspinClient:
    """Feeds one :class:`~.clock.PlaybackClock` from Music Assistant's Sendspin server.

    One client per sync session. A connection carries a single group's metadata,
    so per-session connections keep each area's playhead independent without
    having to multiplex groups — and the expected-title check below means an
    area never adopts another room's playhead even if MA hands every client the
    same group.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        clock: PlaybackClock,
        *,
        name: str = "",
    ) -> None:
        self._hass = hass
        self._url = url
        self._clock = clock
        self._name = name or url
        self._filter = ClockFilter()
        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._client_id = f"synco-{uuid.uuid4().hex[:12]}"
        self._status = STATUS_OFF
        self._expect_title: str | None = None
        self._track_key: str | None = None
        self._anchors = 0
        self._mismatches = 0
        self._last_error: str | None = None
        self._group_id: str | None = None
        self._exchanges = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._status = STATUS_CONNECTING
        self._task = self._hass.async_create_background_task(
            self._run(), f"synco sendspin {self._name}"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._status = STATUS_OFF
        self._ws = None

    # -- state ---------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def synced(self) -> bool:
        return self._status == STATUS_SYNCED

    @property
    def url(self) -> str:
        return self._url

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "url": self._url,
            "group_id": self._group_id,
            "anchors": self._anchors,
            "mismatches": self._mismatches,
            "exchanges": self._exchanges,
            "clock_error_ms": (
                None if not self._filter.ready else round(self._filter.error * 1000.0, 2)
            ),
            "error": self._last_error,
        }

    def expect_track(self, title: str | None, track_key: str | None) -> None:
        """Tell the client which song this area's player is on.

        Two jobs. ``title`` filters: anchors that disagree belong to another
        group and are ignored, so an area never adopts another room's playhead
        — the Home Assistant clock covers it until they agree again. Better a
        coarse-but-correct position than an exact one from the wrong song.

        ``track_key`` is the caller's own track identity, used verbatim on the
        anchor. Both feeds must key tracks the same way, or switching between
        them would read as a track change on every switch.
        """
        self._expect_title = title
        self._track_key = track_key

    # -- the connection ------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - never kill the loop
                self._last_error = f"{type(err).__name__}: {err}"
                _LOGGER.debug(
                    "Sendspin %s connection failed: %s", self._name, err, exc_info=True
                )
            self._status = STATUS_CONNECTING
            self._filter.reset()
            self._exchanges = 0
            attempt += 1
            delay = min(_RECONNECT_MAX_S, _RECONNECT_BASE_S * (2 ** min(attempt, 5)))
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=_CONNECT_TIMEOUT_S)
        async with session.ws_connect(
            self._url, heartbeat=_WS_HEARTBEAT_S, timeout=timeout
        ) as ws:
            self._ws = ws
            self._status = STATUS_SYNCING
            await self._send(
                "client/hello",
                {
                    "client_id": self._client_id,
                    "name": f"Hue Synco {self._name}".strip(),
                    "version": 1,
                    # Metadata only: never a player, so MA will not route audio
                    # here and it does not appear as a speaker.
                    "supported_roles": ["metadata@v1"],
                    "device_info": {
                        "product_name": "Hue Synco",
                        "manufacturer": "synco",
                    },
                },
            )
            timer: asyncio.Task | None = None
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                        continue
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    mtype = data.get("type")
                    payload = data.get("payload") or {}
                    if mtype == "server/hello":
                        _LOGGER.debug(
                            "Sendspin %s connected: %s", self._name, payload
                        )
                        if timer is None:
                            # The spec forbids other messages before the
                            # handshake completes, so the clock starts here.
                            timer = asyncio.ensure_future(self._time_loop())
                    elif mtype == "server/time":
                        self._on_time(payload)
                    elif mtype == "server/state":
                        self._on_state(payload)
                    elif mtype == "stream/clear":
                        # An explicit seek/flush: the next anchor is a jump.
                        _LOGGER.debug("Sendspin %s stream/clear", self._name)
                    elif mtype == "group/update":
                        self._on_group(payload)
            finally:
                if timer is not None:
                    timer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await timer
                self._ws = None
                with contextlib.suppress(Exception):
                    await self._send("client/goodbye", {})

    async def _send(self, mtype: str, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        await ws.send_str(json.dumps({"type": mtype, "payload": payload}))

    # -- clock ---------------------------------------------------------------

    async def _time_loop(self) -> None:
        try:
            while True:
                await self._send(
                    "client/time",
                    {"client_transmitted": int(time.monotonic() * 1_000_000)},
                )
                self._exchanges += 1
                await asyncio.sleep(
                    _TIME_FAST_S if self._exchanges <= _TIME_FAST_N else _TIME_SLOW_S
                )
        except asyncio.CancelledError:
            pass

    def _on_time(self, payload: dict[str, Any]) -> None:
        try:
            t1 = float(payload["client_transmitted"]) / 1e6
            t2 = float(payload["server_received"]) / 1e6
            t3 = float(payload["server_transmitted"]) / 1e6
        except (KeyError, TypeError, ValueError):
            return
        self._filter.update(
            client_transmitted=t1,
            server_received=t2,
            server_transmitted=t3,
            client_received=time.monotonic(),
        )
        if self._filter.ready and self._status == STATUS_SYNCING:
            self._status = STATUS_SYNCED
            _LOGGER.debug(
                "Sendspin %s clock synced (error %.2f ms)",
                self._name, self._filter.error * 1000.0,
            )

    def _on_group(self, payload: dict[str, Any]) -> None:
        gid = payload.get("group_id")
        if gid is not None and gid != self._group_id:
            self._group_id = gid
            _LOGGER.debug("Sendspin %s group -> %s", self._name, gid)
        # Deliberately does not touch the clock: Home Assistant's own player
        # state already drives stop/pause for this area, and a `stopped` group
        # we may not even belong to must never blank this room's playhead.
        if payload.get("playback_state") is not None:
            _LOGGER.debug(
                "Sendspin %s playback_state=%s", self._name, payload["playback_state"]
            )

    # -- the anchor ----------------------------------------------------------

    def _on_state(self, payload: dict[str, Any]) -> None:
        prog = parse_progress(payload)
        if prog is None:
            return
        if not self._filter.ready:
            return  # server timestamps are not yet convertible
        if not _same_track(prog.title, self._expect_title):
            # Another group's playhead. Stay on the HA clock rather than
            # syncing this room's lights to a different room's song.
            self._mismatches += 1
            if self._status == STATUS_SYNCED:
                self._status = STATUS_MISMATCH
            return
        if self._status == STATUS_MISMATCH:
            self._status = STATUS_SYNCED

        at_mono = self._filter.to_client(prog.server_us / 1e6)
        # A stamp far outside the local timeline means the sync is wrong; a bad
        # anchor is worse than none, so refuse it rather than jump the show.
        if abs(at_mono - time.monotonic()) > 30.0:
            _LOGGER.debug(
                "Sendspin %s rejecting anchor %.1f s from now", self._name,
                at_mono - time.monotonic(),
            )
            return
        self._anchors += 1
        self._clock.anchor(
            prog.position_s,
            at_mono,
            rate=prog.rate,
            reason="sendspin",
            track_key=self._track_key,
        )
