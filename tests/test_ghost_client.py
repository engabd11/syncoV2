"""The hue-ghost control-API client, against a tiny in-process aiohttp server.

Runs without the HA harness (plain asyncio.run) so it lives with the other
lightweight tests.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from hue_music_sync.ghost.client import (HueGhostClient, HueGhostError, bindings_of,
                                         summarize_status)

STATUS = {
    "version": "2.3.0", "enabled": True, "state": "syncing", "offset_s": 1.5, "intensity": "high",
    "mode": "video", "use_audio": None,
    "follow": {"device": "Living Room / Swiftfin", "seen": True, "playing": True,
               "item": "Show - S01E02 - Ep", "position_s": 812.4, "paused": False},
    "ghost": {"alive": True, "position_s": 813.9},
    "drift_s": 0.03,
    "engine": {"name": "huesync", "connected": True, "state": "syncing", "error": None,
               "use_audio": True, "area_name": "Living room", "bri": 56},
    "source": {"kind": "video", "name": "Show - S01E02 - Ep", "needs_ghost": True, "binding": "apple-tv"},
    "bindings": [
        {"id": "apple-tv", "name": "Apple TV", "source": "jellyfin", "enabled": True, "active": True,
         "area_id": "a1", "area_name": "Living room", "mode": None, "exe": None, "device": "atv",
         "problems": []},
        {"id": "elden-ring", "name": "Elden Ring", "source": "pc", "enabled": False, "active": False,
         "area_id": "a2", "area_name": "Gaming", "mode": "games", "exe": "eldenring.exe",
         "device": None, "problems": []},
    ],
}


class FakeGhost:
    """Records what the client sends; enforces the bearer token like hue-ghost."""

    def __init__(self, token="secret"):
        self.token = token
        self.calls: list[tuple[str, str, dict]] = []
        self.enabled = True

    def _authed(self, request) -> bool:
        return not self.token or request.headers.get("Authorization") == f"Bearer {self.token}"

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        self.calls.append((request.method, request.path, body))
        if request.path == "/health":
            return web.json_response({"ok": True, "version": "2.0.0"})
        if not self._authed(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.path == "/status":
            return web.json_response({**STATUS, "enabled": self.enabled})
        if request.path in ("/on", "/off"):
            self.enabled = request.path == "/on"
            return web.json_response({"ok": True, "enabled": self.enabled})
        if request.path == "/set":
            if body.get("intensity") == "max":
                return web.json_response({"error": "intensity must be one of subtle, moderate, high, extreme"}, status=400)
            return web.json_response({"ok": True, **body})
        return web.json_response({"error": "unknown path"}, status=404)


async def _serve(fake: FakeGhost):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    return runner, port


def run(coro):
    return asyncio.run(coro)


def test_status_on_off_set_with_token():
    async def main():
        import aiohttp
        fake = FakeGhost()
        runner, port = await _serve(fake)
        try:
            async with aiohttp.ClientSession() as session:
                c = HueGhostClient(session, "127.0.0.1", port, "secret")
                st = await c.status()
                assert st["state"] == "syncing"
                await c.set_enabled(False)
                assert fake.enabled is False
                await c.set_enabled(True)
                await c.set_intensity("moderate")
                await c.set_mode("music")
                await c.set_use_audio(True)
                await c.set_use_audio(None)
                await c.set_offset(1.75)
                await c.adjust_brightness(-5)
                await c.set_brightness(40)
                await c.set_brightness(250)          # clamped, not sent as-is
                await c.set_binding("elden-ring", True)
                assert (await c.health())["ok"] is True
        finally:
            await runner.cleanup()
        paths = [(m, p, b) for (m, p, b) in fake.calls]
        assert ("GET", "/status", {}) in paths
        assert ("POST", "/off", {}) in paths and ("POST", "/on", {}) in paths
        assert ("POST", "/set", {"intensity": "moderate"}) in paths
        assert ("POST", "/set", {"mode": "music"}) in paths
        assert ("POST", "/set", {"use_audio": True}) in paths
        assert ("POST", "/set", {"use_audio": None}) in paths      # null = leave it to the app
        assert ("POST", "/set", {"offset_s": 1.75}) in paths
        assert ("POST", "/set", {"brightness_step": -5}) in paths
        assert ("POST", "/set", {"brightness": 40}) in paths
        assert ("POST", "/set", {"brightness": 100}) in paths
        assert ("POST", "/set", {"binding": {"key": "elden-ring", "enabled": True}}) in paths
    run(main())


def test_status_summary_carries_the_brightness_and_what_is_playing():
    s = summarize_status(STATUS)
    assert s["brightness"] == 56 and s["source_kind"] == "video"
    assert s["area"] == "Living room" and s["state"] == "syncing"


def test_bindings_are_read_and_an_older_pc_simply_has_none():
    binds = bindings_of(STATUS)
    assert [b["id"] for b in binds] == ["apple-tv", "elden-ring"]
    assert binds[0]["active"] is True and binds[1]["enabled"] is False
    # hue-ghost before 2.4 does not report them; that must not raise
    assert bindings_of({"state": "idle"}) == []
    assert bindings_of(None) == []
    assert bindings_of({"bindings": "not a list"}) == []


def test_bad_token_and_rejected_value_raise():
    async def main():
        import aiohttp
        fake = FakeGhost()
        runner, port = await _serve(fake)
        try:
            async with aiohttp.ClientSession() as session:
                wrong = HueGhostClient(session, "127.0.0.1", port, "nope")
                with pytest.raises(HueGhostError, match="401"):
                    await wrong.status()
                ok = HueGhostClient(session, "127.0.0.1", port, "secret")
                with pytest.raises(HueGhostError, match="intensity must be"):
                    await ok.set_intensity("max")
        finally:
            await runner.cleanup()
    run(main())


def test_unreachable_host_raises_cleanly():
    async def main():
        import aiohttp
        async with aiohttp.ClientSession() as session:
            c = HueGhostClient(session, "127.0.0.1", 1, "")
            with pytest.raises(HueGhostError, match="unreachable"):
                await c.status()
    run(main())


def test_summarize_status_flattens_and_handles_offline():
    s = summarize_status(STATUS)
    assert s["state"] == "syncing" and s["now_playing"] == "Show - S01E02 - Ep"
    assert s["drift_s"] == 0.03 and s["engine_state"] == "syncing" and s["offset_s"] == 1.5
    # what hue-ghost wants vs what the Hue Sync app is actually set to
    assert s["mode"] == "video" and s["use_audio"] is None
    assert s["app_use_audio"] is True and s["area"] == "Living room"
    assert summarize_status(None) == {"state": "offline"}
