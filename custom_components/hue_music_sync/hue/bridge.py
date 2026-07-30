"""Hue CLIP v2 client: pairing and entertainment configuration management."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

from ..const import DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)

_API_TIMEOUT = aiohttp.ClientTimeout(total=10)


def capture_light_state(light: dict) -> dict:
    """Snapshot the restorable state of a CLIP v2 light resource.

    Keeps on/off and brightness, and exactly one colour mode — the colour
    temperature if the light is currently in white/CT mode, otherwise its xy
    chromaticity — so the light can be put back exactly as it was.
    """
    state: dict = {"id": light["id"], "on": light.get("on", {}).get("on", True)}
    brightness = light.get("dimming", {}).get("brightness")
    if brightness is not None:
        state["brightness"] = brightness
    ct = light.get("color_temperature") or {}
    mirek = ct.get("mirek")
    if mirek is not None:
        state["mirek"] = mirek  # light was in CT/white mode
    else:
        xy = light.get("color", {}).get("xy")
        if xy is not None:
            state["xy"] = xy
    return state


def restore_light_body(state: dict) -> dict:
    """Build the CLIP v2 PUT body that puts a light back to a captured state."""
    body: dict = {"on": {"on": state["on"]}}
    if "brightness" in state:
        body["dimming"] = {"brightness": state["brightness"]}
    if "mirek" in state:
        body["color_temperature"] = {"mirek": state["mirek"]}
    elif "xy" in state:
        body["color"] = {"xy": state["xy"]}
    return body


class LinkButtonNotPressed(Exception):
    """Raised during pairing when the bridge link button has not been pressed."""


class HueBridgeError(Exception):
    """Generic bridge/API error."""


@dataclass(slots=True)
class EntertainmentChannel:
    """One streamable channel within an entertainment configuration."""

    channel_id: int
    x: float
    y: float
    z: float
    # Per-light colour gamut triangle ((red_x, red_y), (green_x, green_y),
    # (blue_x, blue_y)) resolved from the CLIP v2 API. None when the light's
    # gamut is unknown (older firmware, white-only, or resolution failure) —
    # the encoder falls back to its default gamut in that case.
    gamut: tuple[tuple[float, float], ...] | None = None


@dataclass(slots=True)
class EntertainmentConfig:
    """An entertainment area as exposed by CLIP v2."""

    id: str
    name: str
    status: str
    channels: list[EntertainmentChannel] = field(default_factory=list)
    # "screen" or "room" — affects spatial effect mapping. Screen-type areas
    # have lamp positions relative to a screen + user position.
    configuration_type: str = "room"
    # Which application is currently streaming to this area (per the
    # active_streamer field), or None if inactive. Used for conflict detection.
    active_streamer: str | None = None

    @property
    def is_streaming(self) -> bool:
        return self.status == "active"


async def create_app_key(
    session: aiohttp.ClientSession, host: str, ssl_ctx
) -> tuple[str, str]:
    """Pair with the bridge, returning ``(app_key, client_key)``.

    The link button on the bridge must have been pressed within the last ~30s.
    Uses the legacy ``/api`` endpoint, which still mints the ``clientkey`` (PSK)
    needed for entertainment streaming.
    """
    url = f"https://{host}/api"
    payload = {"devicetype": DEFAULT_NAME, "generateclientkey": True}
    async with session.post(url, json=payload, ssl=ssl_ctx, timeout=_API_TIMEOUT) as resp:
        data = await resp.json(content_type=None)

    if not isinstance(data, list) or not data:
        raise HueBridgeError(f"Unexpected pairing response: {data!r}")
    entry = data[0]
    if "error" in entry:
        err = entry["error"]
        if err.get("type") == 101:
            raise LinkButtonNotPressed
        raise HueBridgeError(err.get("description", str(err)))
    success = entry.get("success", {})
    app_key = success.get("username")
    client_key = success.get("clientkey")
    if not app_key or not client_key:
        raise HueBridgeError(f"Pairing succeeded but key missing: {success!r}")
    return app_key, client_key


async def fetch_application_id(
    session: aiohttp.ClientSession, host: str, app_key: str, ssl_ctx
) -> str | None:
    """Fetch the hue-application-id from the bridge's /auth/v1 endpoint.

    Per the Hue Entertainment API spec, the PSK identity for the DTLS handshake
    must be the ``hue-application-id`` (not the app key / username). This is
    retrieved via a GET on ``https://<bridge>/auth/v1`` with the
    ``hue-application-key`` header; the bridge responds with a
    ``hue-application-id`` response header.

    Returns None if the bridge does not expose the endpoint (older firmware) so
    callers can fall back to the app key as before.
    """
    url = f"https://{host}/auth/v1"
    headers = {"hue-application-key": app_key}
    try:
        async with session.get(
            url, headers=headers, ssl=ssl_ctx, timeout=_API_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return None
            # The application id is in a response header, not the body.
            app_id = resp.headers.get("hue-application-id")
            if app_id:
                return app_id
    except (aiohttp.ClientError, OSError):
        return None
    return None


class HueBridge:
    """Authenticated CLIP v2 client for one bridge."""

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

    @property
    def _headers(self) -> dict[str, str]:
        return {"hue-application-key": self._app_key}

    def _url(self, path: str) -> str:
        return f"https://{self._host}/clip/v2/resource/{path}"

    async def _get(self, path: str) -> list[dict]:
        async with self._session.get(
            self._url(path), headers=self._headers, ssl=self._ssl, timeout=_API_TIMEOUT
        ) as resp:
            if resp.status == 403:
                raise HueBridgeError("Bridge rejected the application key (403)")
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        if body.get("errors"):
            raise HueBridgeError(str(body["errors"]))
        return body.get("data", [])

    async def get_light_gamuts(self) -> dict[str, tuple[tuple[float, float], ...]]:
        """Per-light colour gamut triangles from the CLIP v2 API.

        The API exposes each light's gamut as ``color.gamut`` with
        ``red/green/blue`` xy points. Returns a mapping of light resource id
        to a ``(red, green, blue)`` tuple of ``(x, y)`` pairs. Lights without
        a color gamut (white-only) are omitted.
        """
        gamuts: dict[str, tuple[tuple[float, float], ...]] = {}
        for light in await self._get("light"):
            color = light.get("color")
            if color is None:
                continue
            gamut = color.get("gamut")
            if gamut is None:
                continue
            try:
                red = (float(gamut["red"]["x"]), float(gamut["red"]["y"]))
                green = (float(gamut["green"]["x"]), float(gamut["green"]["y"]))
                blue = (float(gamut["blue"]["x"]), float(gamut["blue"]["y"]))
                gamuts[light["id"]] = (red, green, blue)
            except (KeyError, TypeError, ValueError):
                continue
        return gamuts

    async def get_entertainment_configs(self) -> list[EntertainmentConfig]:
        """List entertainment areas with their channel positions.

        Also fetches per-light gamuts and resolves each channel's light to
        its gamut, so the stream encoder can clamp to the correct colour
        triangle per lamp instead of a single hardcoded gamut.
        """
        # Fetch gamuts once and build the channel→gamut map alongside the
        # entertainment configs. The gamut resolution follows the same path
        # as _area_light_rids: channel members → entertainment services →
        # devices → light resources.
        light_gamuts = await self.get_light_gamuts()

        # Build entertainment service → device mapping for gamut resolution.
        ent_services: dict[str, str] = {}  # ent_service_id -> device_rid
        for ent in await self._get("entertainment"):
            owner = ent.get("owner", {})
            if owner.get("rtype") == "device":
                ent_services[ent["id"]] = owner["rid"]

        # Build device → light resource id mapping.
        device_lights: dict[str, str] = {}  # device_rid -> light_rid
        for light in await self._get("light"):
            owner = light.get("owner", {})
            if owner.get("rtype") == "device":
                device_lights[owner["rid"]] = light["id"]

        configs: list[EntertainmentConfig] = []
        for item in await self._get("entertainment_configuration"):
            channels = []
            for ch in item.get("channels", []):
                channel = EntertainmentChannel(
                    channel_id=ch["channel_id"],
                    x=ch.get("position", {}).get("x", 0.0),
                    y=ch.get("position", {}).get("y", 0.0),
                    z=ch.get("position", {}).get("z", 0.0),
                )
                # Resolve this channel's gamut from its member light.
                for member in ch.get("members", []):
                    svc = member.get("service", {})
                    if svc.get("rtype") != "entertainment":
                        continue
                    ent_id = svc.get("rid")
                    device_rid = ent_services.get(ent_id or "")
                    light_rid = device_lights.get(device_rid or "")
                    if light_rid and light_rid in light_gamuts:
                        channel.gamut = light_gamuts[light_rid]
                    break
                channels.append(channel)
            configs.append(
                EntertainmentConfig(
                    id=item["id"],
                    name=item.get("metadata", {}).get("name", item["id"]),
                    status=item.get("status", "inactive"),
                    channels=channels,
                    configuration_type=item.get("configuration_type", "room"),
                    active_streamer=item.get("active_streamer"),
                )
            )
        return configs

    async def get_entertainment_config(self, config_id: str) -> EntertainmentConfig:
        for cfg in await self.get_entertainment_configs():
            if cfg.id == config_id:
                return cfg
        raise HueBridgeError(f"Entertainment configuration {config_id} not found")

    async def _set_action(self, config_id: str, action: str) -> None:
        url = self._url(f"entertainment_configuration/{config_id}")
        async with self._session.put(
            url,
            headers=self._headers,
            json={"action": action},
            ssl=self._ssl,
            timeout=_API_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        if body.get("errors"):
            raise HueBridgeError(str(body["errors"]))

    async def start_stream(self, config_id: str) -> None:
        """Hand the entertainment area over to streaming mode."""
        await self._set_action(config_id, "start")

    async def stop_stream(self, config_id: str) -> None:
        """Return control of the area to the bridge (restores prior light state)."""
        await self._set_action(config_id, "stop")

    async def _put(self, path: str, body: dict) -> None:
        async with self._session.put(
            self._url(path), headers=self._headers, json=body,
            ssl=self._ssl, timeout=_API_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if data.get("errors"):
            raise HueBridgeError(str(data["errors"]))

    async def _area_light_rids(self, config_id: str) -> set[str]:
        """Resolve the light resource ids that belong to an entertainment area."""
        items = await self._get(f"entertainment_configuration/{config_id}")
        item = items[0] if items else {}
        # Some bridges still expose the direct (deprecated) light_services list.
        rids = {
            s["rid"]
            for s in item.get("light_services", [])
            if s.get("rtype") == "light"
        }
        if rids:
            return rids
        # Otherwise map channel members -> entertainment services -> owner
        # devices -> the light service on each device.
        ent_rids = {
            m["service"]["rid"]
            for ch in item.get("channels", [])
            for m in ch.get("members", [])
            if m.get("service", {}).get("rtype") == "entertainment"
        }
        if not ent_rids:
            return set()
        device_rids = {
            e["owner"]["rid"]
            for e in await self._get("entertainment")
            if e.get("id") in ent_rids and e.get("owner", {}).get("rtype") == "device"
        }
        return {
            light["id"]
            for light in await self._get("light")
            if light.get("owner", {}).get("rid") in device_rids
        }

    async def snapshot_area_lights(self, config_id: str) -> list[dict]:
        """Capture the current state of every light in an entertainment area."""
        rids = await self._area_light_rids(config_id)
        if not rids:
            return []
        return [
            capture_light_state(light)
            for light in await self._get("light")
            if light["id"] in rids
        ]

    async def restore_light_states(self, states: list[dict], passes: int = 2) -> None:
        """Put each light back to a captured state, retrying to beat Zigbee loss.

        The bridge's own restore-on-stop occasionally drops a single light's
        command, so we re-apply our snapshot ourselves a couple of times.
        """
        for attempt in range(passes):
            for state in states:
                try:
                    await self._put(f"light/{state['id']}", restore_light_body(state))
                except (HueBridgeError, OSError) as err:
                    _LOGGER.debug("Restore of light %s failed: %s", state.get("id"), err)
            if attempt < passes - 1:
                await asyncio.sleep(0.3)
