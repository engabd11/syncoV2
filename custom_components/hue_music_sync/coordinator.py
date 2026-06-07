"""Session lifecycle: tie audio -> analysis -> choreography -> DTLS per area.

``SyncManager`` is created once per config entry and owns the bridge connection,
the per-area settings, and one ``SyncSession`` per area that is actively syncing.
A session runs a render loop paced by the audio source: it pulls a hop, extracts
features, renders per-channel colour and streams a HueStream frame. When nothing
is playing it emits a gentle idle glow and keeps probing for playback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .audio.analyzer import AnalysisFrame
from .audio.metadata import MetadataSource
from .audio.snapcast import SnapcastSource
from .audio.source import MusicAssistantSource
from .color.album_art import extract_palette
from .const import (
    CONF_AREAS,
    CONF_BRIGHTNESS,
    CONF_COLOUR,
    CONF_LATENCY_MS,
    CONF_MEDIA_PLAYER,
    CONF_MODE,
    CONF_SNAPSERVER_HOST,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOUR,
    DEFAULT_LATENCY_MS,
    DEFAULT_MODE,
    DEFAULT_STREAM_FPS,
    ColorScheme,
    SyncMode,
    signal_area_update,
)
from .effects.engine import EffectEngine
from .hue.bridge import EntertainmentConfig, HueBridge
from .hue.stream import DtlsStream, HueStreamEncoder

_LOGGER = logging.getLogger(__name__)

_IDLE_FPS = 10
_IDLE_REOPEN_S = 2.0


@dataclass(slots=True)
class AreaSettings:
    """Per-area settings. The user only picks a ``mode``; scheme/effect/intensity
    are derived from the preset, keeping the UI Samsung-simple. ``media_player``
    and ``latency_ms`` are advanced options (auto/default by default)."""

    mode: SyncMode = DEFAULT_MODE
    colour: ColorScheme = DEFAULT_COLOUR
    brightness: float = DEFAULT_BRIGHTNESS
    media_player: str | None = None
    latency_ms: int = DEFAULT_LATENCY_MS

    @classmethod
    def from_dict(cls, data: dict) -> AreaSettings:
        try:
            mode = SyncMode(data.get(CONF_MODE, DEFAULT_MODE))
        except ValueError:
            mode = DEFAULT_MODE
        try:
            colour = ColorScheme(data.get(CONF_COLOUR, DEFAULT_COLOUR))
        except ValueError:
            colour = DEFAULT_COLOUR
        return cls(
            mode=mode,
            colour=colour,
            brightness=float(data.get(CONF_BRIGHTNESS, DEFAULT_BRIGHTNESS)),
            media_player=data.get(CONF_MEDIA_PLAYER),
            latency_ms=int(data.get(CONF_LATENCY_MS, DEFAULT_LATENCY_MS)),
        )

    def to_dict(self) -> dict:
        return {
            CONF_MODE: str(self.mode),
            CONF_COLOUR: str(self.colour),
            CONF_BRIGHTNESS: self.brightness,
            CONF_MEDIA_PLAYER: self.media_player,
            CONF_LATENCY_MS: self.latency_ms,
        }


class SyncSession:
    """Runs music sync for one entertainment area until stopped."""

    def __init__(
        self,
        hass: HomeAssistant,
        bridge: HueBridge,
        host: str,
        app_key: str,
        client_key: str,
        ffmpeg_bin: str,
        config: EntertainmentConfig,
        settings: AreaSettings,
        snapserver_host: str = "",
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._bridge = bridge
        self._ffmpeg = ffmpeg_bin
        self._config = config
        self._settings = settings
        self._snapserver_host = snapserver_host
        self._on_finished = on_finished

        self._encoder = HueStreamEncoder(config.id)
        self._stream = DtlsStream(host, app_key, client_key)
        self._engine = EffectEngine(config.channels)
        self._source: MusicAssistantSource | MetadataSource | SnapcastSource | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._stopping = False
        self._last_track: str | None = None
        self._art_task: asyncio.Task | None = None

    @property
    def settings(self) -> AreaSettings:
        return self._settings

    async def start(self) -> None:
        self._engine.set_mode(self._settings.mode)
        self._engine.set_brightness(self._settings.brightness)
        self._apply_colour()
        await self._bridge.start_stream(self._config.id)
        try:
            await self._stream.start()
        except Exception:
            await self._bridge.stop_stream(self._config.id)
            raise
        self._running = True
        self._task = self._hass.async_create_background_task(
            self._run(), f"hue_music_sync_{self._config.id}"
        )

    def apply_settings(self, settings: AreaSettings) -> None:
        """Live-apply changed settings to the running session."""
        prev = self._settings
        self._settings = settings
        self._engine.set_mode(settings.mode)
        self._engine.set_brightness(settings.brightness)
        if settings.colour != prev.colour:
            self._apply_colour()
            self._last_track = None  # re-extract album art if switching to Album
        if self._source is not None and settings.media_player != prev.media_player:
            # Player changed: drop current source so the loop re-opens it.
            self._hass.async_create_task(self._reset_source())

    def _apply_colour(self) -> None:
        # Preset themes are static palettes; Album uses the engine fallback until
        # album art is extracted for the current track.
        if self._settings.colour != ColorScheme.ALBUM_ART:
            self._engine.set_scheme(self._settings.colour)

    async def _reset_source(self) -> None:
        if self._source is not None:
            await self._source.close()
            self._source = None
        self._last_track = None

    def _resolve_player(self) -> str | None:
        if self._settings.media_player:
            return self._settings.media_player
        # Auto-pick a currently-playing player, preferring Music Assistant ones.
        registry = er.async_get(self._hass)
        playing = [
            s.entity_id
            for s in self._hass.states.async_all("media_player")
            if s.state == "playing"
        ]
        _LOGGER.debug("Playing media_players: %s", playing)
        for entity_id in playing:
            entry = registry.async_get(entity_id)
            if entry is not None and entry.platform == "music_assistant":
                _LOGGER.debug("Following Music Assistant player %s", entity_id)
                return entity_id
        if playing:
            _LOGGER.debug("No MA player playing; following %s", playing[0])
        return playing[0] if playing else None

    async def _ensure_source(self) -> bool:
        if self._source is not None:
            return True
        entity_id = self._resolve_player()
        if entity_id is None:
            return False

        # 1. Snapcast tap (primary): real, beat-accurate audio for any MA player.
        if self._snapserver_host:
            entry = er.async_get(self._hass).async_get(entity_id)
            player_uid = entry.unique_id if entry else None
            snap = SnapcastSource(
                self._hass, entity_id, self._snapserver_host, self._ffmpeg, player_uid
            )
            if await snap.open():
                self._source = snap
                return True
            await snap.close()

        # 2. Music Assistant HTTP tap (works for players that expose a stream).
        ma = MusicAssistantSource(
            self._hass, entity_id, self._ffmpeg, self._settings.latency_ms
        )
        if await ma.open():
            self._source = ma
            return True
        await ma.close()

        # 3. Metadata-driven animation (universal fallback).
        meta = MetadataSource(self._hass, entity_id)
        if await meta.open():
            _LOGGER.info(
                "No tappable audio for %s; using metadata-driven sync", entity_id
            )
            self._source = meta
            return True
        return False

    async def _run(self) -> None:
        idle_color_phase = 0.0
        last_t = time.monotonic()
        last_reopen = 0.0
        period = 1.0 / DEFAULT_STREAM_FPS
        try:
            while self._running:
                now = time.monotonic()
                dt = now - last_t
                last_t = now

                if self._source is None:
                    if now - last_reopen >= _IDLE_REOPEN_S:
                        last_reopen = now
                        await self._ensure_source()
                    if self._source is None:
                        idle_color_phase += dt * 0.05
                        await self._send_idle(idle_color_phase)
                        await asyncio.sleep(1.0 / _IDLE_FPS)
                        continue

                frame = await self._source.read_frame()  # paced to real time
                if frame is None:
                    await self._reset_source()
                    continue

                self._maybe_refresh_album_art()
                colors = self._engine.render(frame, period)
                await self._safe_send(colors)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Music sync loop crashed for %s", self._config.name)
        finally:
            # If the loop ended on its own (DTLS dropped, crash) rather than via
            # stop(), let the manager reconcile state and refresh the switch.
            if not self._stopping and self._on_finished is not None:
                self._on_finished()

    async def _send_idle(self, phase: float) -> None:
        # Gentle dim glow drifting through the palette while waiting for audio.
        idle = AnalysisFrame(bands={b: 0.0 for b in ("sub_bass", "bass", "low_mid", "mid", "high")},
                             energy=0.08)
        self._engine.color_phase = phase
        colors = self._engine.render(idle, 1.0 / _IDLE_FPS)
        await self._safe_send(colors)

    async def _safe_send(self, colors: dict[int, tuple[float, float, float]]) -> None:
        frame = self._encoder.build(colors)
        try:
            await self._stream.send(frame)
        except ConnectionError:
            _LOGGER.warning("DTLS channel lost for %s; stopping sync", self._config.name)
            self._running = False

    def _maybe_refresh_album_art(self) -> None:
        # Only when the Album colour is selected; preset themes are static.
        if self._settings.colour != ColorScheme.ALBUM_ART or self._source is None:
            return
        track = self._source.track_id
        if track == self._last_track:
            return
        self._last_track = track
        url = self._source.album_art_url
        if not url:
            return
        if self._art_task and not self._art_task.done():
            return
        self._art_task = self._hass.async_create_task(self._extract_art(url))

    async def _extract_art(self, url: str) -> None:
        palette = await extract_palette(self._ffmpeg, url)
        if palette is not None and self._settings.colour == ColorScheme.ALBUM_ART:
            self._engine.set_palette(palette)

    async def stop(self) -> None:
        self._stopping = True
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._source is not None:
            await self._source.close()
            self._source = None
        await self._stream.stop()
        try:
            await self._bridge.stop_stream(self._config.id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error stopping bridge stream for %s: %s", self._config.name, err)


class SyncManager:
    """Owns the bridge, per-area settings and active sessions for one entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        bridge: HueBridge,
        host: str,
        app_key: str,
        client_key: str,
        ffmpeg_bin: str,
        configs: list[EntertainmentConfig],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.bridge = bridge
        self._host = host
        self._app_key = app_key
        self._client_key = client_key
        self._ffmpeg = ffmpeg_bin
        self.configs: dict[str, EntertainmentConfig] = {c.id: c for c in configs}
        self.enabled_areas: list[str] = list(entry.data.get(CONF_AREAS, []))
        self._snapserver_host: str = entry.options.get(CONF_SNAPSERVER_HOST, "") or ""
        self._sessions: dict[str, SyncSession] = {}
        self._settings: dict[str, AreaSettings] = self._load_settings()

    def _load_settings(self) -> dict[str, AreaSettings]:
        stored = self.entry.options.get("area_settings", {})
        out: dict[str, AreaSettings] = {}
        for area_id in self.enabled_areas:
            out[area_id] = AreaSettings.from_dict(stored.get(area_id, {}))
        return out

    async def _persist_settings(self) -> None:
        options = dict(self.entry.options)
        options["area_settings"] = {
            aid: s.to_dict() for aid, s in self._settings.items()
        }
        self.hass.config_entries.async_update_entry(self.entry, options=options)

    def get_settings(self, area_id: str) -> AreaSettings:
        return self._settings.get(area_id, AreaSettings())

    def is_active(self, area_id: str) -> bool:
        return area_id in self._sessions

    async def update_settings(self, area_id: str, **changes) -> None:
        current = self.get_settings(area_id)
        updated = replace(current, **changes)
        self._settings[area_id] = updated
        if (session := self._sessions.get(area_id)) is not None:
            session.apply_settings(updated)
        await self._persist_settings()

    async def start_area(self, area_id: str) -> None:
        if area_id in self._sessions:
            return
        config = self.configs.get(area_id)
        if config is None:
            raise ValueError(f"Unknown entertainment area {area_id}")
        # A Hue bridge streams to only one entertainment area at a time, so
        # gracefully take over from any area already syncing on this bridge.
        for other_id in list(self._sessions):
            _LOGGER.info(
                "Taking over Hue stream from area %s for %s", other_id, area_id
            )
            await self.stop_area(other_id)
        # Refresh channels/status in case the area changed in the Hue app.
        config = await self.bridge.get_entertainment_config(area_id)
        self.configs[area_id] = config
        session = SyncSession(
            self.hass, self.bridge, self._host, self._app_key, self._client_key,
            self._ffmpeg, config, self.get_settings(area_id),
            snapserver_host=self._snapserver_host,
            on_finished=lambda: self._on_session_finished(area_id),
        )
        await session.start()
        self._sessions[area_id] = session

    def _on_session_finished(self, area_id: str) -> None:
        """A session ended on its own (e.g. lost DTLS); drop it and refresh."""
        self._sessions.pop(area_id, None)
        async_dispatcher_send(self.hass, signal_area_update(area_id))

    async def stop_area(self, area_id: str) -> None:
        session = self._sessions.pop(area_id, None)
        if session is not None:
            await session.stop()
        async_dispatcher_send(self.hass, signal_area_update(area_id))

    async def async_shutdown(self) -> None:
        for area_id in list(self._sessions):
            await self.stop_area(area_id)
