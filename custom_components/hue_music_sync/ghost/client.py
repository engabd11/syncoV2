"""HTTP client for hue-ghost's control API. No Home Assistant imports (unit
tested in ``tests/`` without the HA harness).

  GET  /status                        -> full state
  GET  /health                        -> {"ok": true, "version": ...}
  POST /on | /off
  POST /set  {"mode": ..., "intensity": ..., "use_audio": ..., "offset_s": ...,
              "offset_delta": ..., "brightness_step": ..., "brightness": 0-100,
              "binding": {"key": ..., "enabled": ...}}

Auth: ``Authorization: Bearer <token>`` when the PC has a token configured.
"""

from __future__ import annotations

from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=5)

GHOST_STATES = ("offline", "idle", "ghosting", "syncing")


class HueGhostError(Exception):
    """hue-ghost unreachable or rejected the request."""


class HueGhostClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int = 8787,
                 token: str = "") -> None:
        self._session = session
        self._base = f"http://{host}:{int(port)}"
        self._token = token or ""

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            async with self._session.request(
                method, self._base + path, json=body, headers=self._headers(),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    raise HueGhostError("hue-ghost rejected the token (401)")
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    msg = data.get("error") if isinstance(data, dict) else resp.status
                    raise HueGhostError(f"hue-ghost {path}: {msg}")
                return data if isinstance(data, dict) else {}
        except aiohttp.ClientError as err:
            raise HueGhostError(f"hue-ghost unreachable at {self._base}: {err}") from err
        except TimeoutError as err:
            raise HueGhostError(f"hue-ghost timed out at {self._base}") from err

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/status")

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def set_enabled(self, on: bool) -> dict[str, Any]:
        return await self._request("POST", "/on" if on else "/off", {})

    async def set_intensity(self, level: str) -> dict[str, Any]:
        return await self._request("POST", "/set", {"intensity": level})

    async def set_mode(self, mode: str) -> dict[str, Any]:
        return await self._request("POST", "/set", {"mode": mode})

    async def set_use_audio(self, on: bool | None) -> dict[str, Any]:
        """True/False to enforce Hue Sync's "use audio for light effects",
        None to leave whatever the app itself is set to."""
        return await self._request("POST", "/set", {"use_audio": on})

    async def set_offset(self, seconds: float) -> dict[str, Any]:
        return await self._request("POST", "/set", {"offset_s": round(float(seconds), 3)})

    async def adjust_brightness(self, step: int) -> dict[str, Any]:
        return await self._request("POST", "/set", {"brightness_step": int(step)})

    async def set_brightness(self, level: int) -> dict[str, Any]:
        """Absolute 0-100. Hue Sync's protocol only has a signed step, so
        hue-ghost synthesises this from the level the app reports."""
        return await self._request("POST", "/set", {"brightness": max(0, min(100, int(level)))})

    async def set_binding(self, key: str, on: bool) -> dict[str, Any]:
        """Follow one source, or stop following it."""
        return await self._request("POST", "/set", {"binding": {"key": key, "enabled": bool(on)}})


def summarize_status(status: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten hue-ghost's /status into sensor attributes (stable key names)."""
    if not status:
        return {"state": "offline"}
    follow = status.get("follow") or {}
    ghost = status.get("ghost") or {}
    engine = status.get("engine") or {}
    return {
        "state": status.get("state") or "idle",
        "enabled": bool(status.get("enabled")),
        "now_playing": follow.get("item"),
        "position_s": follow.get("position_s"),
        "paused": follow.get("paused"),
        "followed_device": follow.get("device") or follow.get("device_name_contains"),
        "client_connected": bool(follow.get("seen")),
        "drift_s": status.get("drift_s"),
        "ghost_position_s": ghost.get("position_s"),
        "engine": engine.get("name"),
        "engine_connected": engine.get("connected"),
        "engine_state": engine.get("state"),
        "engine_error": engine.get("error"),
        "mode": status.get("mode"),
        "intensity": status.get("intensity"),
        "use_audio": status.get("use_audio"),
        "app_use_audio": engine.get("use_audio"),
        "area": engine.get("area_name"),
        "offset_s": status.get("offset_s"),
        "brightness": engine.get("bri"),
        "source_kind": (status.get("source") or {}).get("kind"),
        "version": status.get("version"),
    }


def bindings_of(status: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every source hue-ghost can follow. Older versions do not report them."""
    if not status:
        return []
    out = status.get("bindings")
    return [b for b in out if isinstance(b, dict) and b.get("id")] if isinstance(out, list) else []
