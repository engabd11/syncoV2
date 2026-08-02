"""CLIP v2 Server-Sent Events client — how we learn about bridge-side changes.

Hue's Core Concepts guide is explicit that polling is the wrong way to track
state: *"It is important to not try to stay up to date by performing repeated
GET requests, as that is bad for the performance of the system and generally
gives a laggy user experience. Instead, you can subscribe to retrieving
proactive event notifications."*

For this integration the event that matters is an ``entertainment_configuration``
going ``inactive``, or its ``active_streamer`` changing to somebody else — the
Hue app (or any other application) taking the area back. Without this the only
symptom is our own DTLS session dying, which is indistinguishable from a Wi-Fi
glitch, and the reconnect logic would cheerfully seize the area straight back.

The bridge coalesces changes into at most one container per second, so an event
is "the current state of what changed", never a complete history — every handler
here treats it as a hint to act on, not as a log to replay.

Home Assistant's shared aiohttp session speaks HTTP/1.1, so this holds one extra
long-lived connection to the bridge alongside the CLIP request pool — which is
what the guide expects on 1.1 ("you will need a separate connection for the SSE
request"). It is a single idle socket, not a poll loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import aiohttp

from ..const import (
    EVENTSTREAM_PATH,
    EVENTSTREAM_RECONNECT_BASE_S,
    EVENTSTREAM_RECONNECT_MAX_S,
)

_LOGGER = logging.getLogger(__name__)

# Connect must not hang forever, but reads legitimately idle for hours between
# events, so there is deliberately no read timeout.
_CONNECT_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, connect=10)


def parse_event_payload(payload: str) -> list[dict]:
    """Flatten one SSE ``data:`` payload into the changed resource dicts.

    The wire format is a JSON array of *containers*, each with its own ``data``
    array of resources (multiple resources are grouped into one container when
    they change inside the same one-second window). Callers only ever care about
    the resources, so the container layer is flattened away here.

    Malformed payloads return an empty list rather than raising: a parser error
    must never be able to kill the subscription.
    """
    try:
        containers = json.loads(payload)
    except ValueError:
        _LOGGER.debug("Ignoring unparseable event payload: %.200r", payload)
        return []
    if not isinstance(containers, list):
        return []
    resources: list[dict] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        for resource in container.get("data") or ():
            if isinstance(resource, dict):
                resources.append(resource)
    return resources


class HueEventStream:
    """Long-lived subscription to ``/eventstream/clip/v2`` for one bridge.

    One instance per bridge, shared by every area. Listeners are registered by
    resource type and receive each changed resource dict as it arrives.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        app_key: str,
        ssl_ctx,
    ) -> None:
        self._session = session
        self._host = host
        self._app_key = app_key
        self._ssl = ssl_ctx
        self._listeners: dict[str, list[Callable[[dict], None]]] = {}
        self._task: asyncio.Task | None = None
        self._closed = False
        self.connected = False

    def subscribe(
        self, rtype: str, callback: Callable[[dict], None]
    ) -> Callable[[], None]:
        """Listen for changes to one resource type; returns an unsubscribe."""
        self._listeners.setdefault(rtype, []).append(callback)

        def _unsubscribe() -> None:
            handlers = self._listeners.get(rtype)
            if handlers and callback in handlers:
                handlers.remove(callback)

        return _unsubscribe

    def start(self) -> None:
        if self._task is None:
            self._closed = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._closed = True
        self.connected = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        """Keep the subscription up, reconnecting with capped backoff."""
        attempt = 0
        while not self._closed:
            try:
                await self._listen()
                attempt = 0  # a clean end means the connection did work
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - never let the loop die
                attempt += 1
                _LOGGER.debug(
                    "Hue event stream for %s dropped (attempt %d): %s",
                    self._host, attempt, err,
                )
            finally:
                self.connected = False
            if self._closed:
                return
            await asyncio.sleep(
                min(
                    EVENTSTREAM_RECONNECT_BASE_S * attempt,
                    EVENTSTREAM_RECONNECT_MAX_S,
                )
                if attempt
                else EVENTSTREAM_RECONNECT_BASE_S
            )

    async def _listen(self) -> None:
        """One connection's lifetime: read events until the stream ends."""
        url = f"https://{self._host}{EVENTSTREAM_PATH}"
        headers = {
            "hue-application-key": self._app_key,
            "Accept": "text/event-stream",
        }
        async with self._session.get(
            url, headers=headers, ssl=self._ssl, timeout=_CONNECT_TIMEOUT
        ) as resp:
            if resp.status == 403:
                raise PermissionError("Bridge rejected the application key (403)")
            resp.raise_for_status()
            self.connected = True
            _LOGGER.debug("Hue event stream connected to %s", self._host)
            # SSE frames the payload as one or more `data:` lines terminated by
            # a blank line; anything else (`id:`, `:` comments) is ignored.
            payload: list[str] = []
            async for raw in resp.content:
                if self._closed:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if payload:
                        self._dispatch("\n".join(payload))
                        payload = []
                    continue
                if line.startswith("data:"):
                    payload.append(line[5:].lstrip())
            if payload:
                self._dispatch("\n".join(payload))

    def _dispatch(self, payload: str) -> None:
        for resource in parse_event_payload(payload):
            for callback in list(self._listeners.get(resource.get("type", ""), ())):
                try:
                    callback(resource)
                except Exception:  # noqa: BLE001 - one bad listener must not
                    # break the subscription for every other area.
                    _LOGGER.exception("Hue event listener failed")
