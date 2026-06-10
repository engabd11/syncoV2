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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .audio.metadata import MetadataSource
from .audio.snapcast import SnapcastSource
from .audio.source import MusicAssistantSource
from .audio.structure import StructureTracker
from .audio.tempo import BeatGrid, TempoTracker
from .color.album_art import extract_palette
from .const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    CONF_AREAS,
    CONF_BRIGHTNESS,
    CONF_COLOUR,
    CONF_EFFECT,
    CONF_LATENCY_MS,
    CONF_MEDIA_PLAYER,
    CONF_MODE,
    CONF_RESTORE_LIGHTS,
    CONF_SNAPSERVER_HOST,
    CONF_TIMING_MS,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOUR,
    DEFAULT_EFFECT,
    DEFAULT_LATENCY_MS,
    DEFAULT_MODE,
    DEFAULT_RESTORE_LIGHTS,
    DEFAULT_STREAM_FPS,
    DEFAULT_TIMING_MS,
    DOMAIN,
    TIMING_BUFFER_MS,
    ColorScheme,
    SyncEffect,
    SyncMode,
    signal_area_update,
)
from .effects.engine import EffectEngine
from .effects.safety import FieldSafety
from .hue.bridge import EntertainmentConfig, HueBridge
from .hue.stream import DtlsStream, HueStreamEncoder

_LOGGER = logging.getLogger(__name__)

_IDLE_FPS = 10
_IDLE_REOPEN_S = 2.0

# DTLS auto-reconnect: a dropped channel (transient packet loss, bridge hiccup)
# should recover on its own instead of silently ending the session.
_RECONNECT_ATTEMPTS = 5
_RECONNECT_BASE_S = 1.0
_RECONNECT_MAX_S = 8.0


def _circular_distance(a: float, b: float, period: float) -> float:
    """Smallest distance between two phases on a cyclic timeline of ``period``."""
    d = abs(a - b) % period
    return min(d, period - d)


@dataclass(slots=True)
class AreaSettings:
    """Per-area settings. The user only picks a ``mode``; scheme/effect/intensity
    are derived from the preset, keeping the UI Samsung-simple. ``media_player``
    and ``latency_ms`` are advanced options (auto/default by default)."""

    mode: SyncMode = DEFAULT_MODE
    effect: SyncEffect = DEFAULT_EFFECT
    colour: ColorScheme = DEFAULT_COLOUR
    brightness: float = DEFAULT_BRIGHTNESS
    timing_ms: int = DEFAULT_TIMING_MS
    media_player: str | None = None
    latency_ms: int = DEFAULT_LATENCY_MS

    @classmethod
    def from_dict(cls, data: dict) -> AreaSettings:
        try:
            mode = SyncMode(data.get(CONF_MODE, DEFAULT_MODE))
        except ValueError:
            mode = DEFAULT_MODE
        try:
            effect = SyncEffect(data.get(CONF_EFFECT, DEFAULT_EFFECT))
        except ValueError:
            effect = DEFAULT_EFFECT
        try:
            colour = ColorScheme(data.get(CONF_COLOUR, DEFAULT_COLOUR))
        except ValueError:
            colour = DEFAULT_COLOUR
        return cls(
            mode=mode,
            effect=effect,
            colour=colour,
            brightness=float(data.get(CONF_BRIGHTNESS, DEFAULT_BRIGHTNESS)),
            timing_ms=int(data.get(CONF_TIMING_MS, DEFAULT_TIMING_MS)),
            media_player=data.get(CONF_MEDIA_PLAYER),
            latency_ms=int(data.get(CONF_LATENCY_MS, DEFAULT_LATENCY_MS)),
        )

    def to_dict(self) -> dict:
        return {
            CONF_MODE: str(self.mode),
            CONF_EFFECT: str(self.effect),
            CONF_COLOUR: str(self.colour),
            CONF_BRIGHTNESS: self.brightness,
            CONF_TIMING_MS: self.timing_ms,
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
        restore_lights: bool = False,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._bridge = bridge
        self._ffmpeg = ffmpeg_bin
        self._config = config
        self._settings = settings
        self._snapserver_host = snapserver_host
        self._restore_lights = restore_lights
        self._light_snapshot: list[dict] | None = None
        self._on_finished = on_finished

        self._encoder = HueStreamEncoder(config.id)
        self._stream = DtlsStream(host, app_key, client_key)
        self._engine = EffectEngine(config.channels)
        # Non-bypassable final safety stage (whole-field flash limiter + red
        # guard); every emitted frame — any effect, intensity or idle — passes
        # through it in _safe_send.
        self._safety = FieldSafety()
        self._last_safe_t: float | None = None
        # Predictive beat grid + musical-structure trackers, fed the analyzer's
        # feature stream so the engine can anticipate beats and ride builds/drops.
        _period = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE
        self._tempo = TempoTracker(_period)
        self._structure = StructureTracker(_period)
        self._source: MusicAssistantSource | MetadataSource | SnapcastSource | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._stopping = False
        self._last_track: str | None = None
        self._art_task: asyncio.Task | None = None
        self._delay_buf: deque[tuple[float, dict]] = deque()
        # Now-playing / album-colour / tempo snapshot surfaced on the switch entity
        # so dashboard cards can recolour and lock a visualizer to the song.
        self.public_state: dict = {}
        self._album_hex: list[str] = []
        self._last_beatgrid: BeatGrid | None = None
        self._beat_anchor: float | None = None  # stable downbeat ref for the card
        self._last_publish = 0.0

    @property
    def settings(self) -> AreaSettings:
        return self._settings

    async def start(self) -> None:
        self._engine.set_mode(self._settings.mode)
        self._engine.set_effect(self._settings.effect)
        self._engine.set_brightness(self._settings.brightness)
        self._apply_colour()
        # Snapshot the area's lights *before* streaming so we can restore their
        # exact pre-sync state on stop (opt-in; covers the occasional light the
        # bridge's own restore misses).
        if self._restore_lights:
            try:
                self._light_snapshot = await self._bridge.snapshot_area_lights(
                    self._config.id
                )
            except Exception as err:  # noqa: BLE001 - best-effort, never block start
                _LOGGER.debug("Light snapshot for %s failed: %s", self._config.name, err)
                self._light_snapshot = None
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
        self._engine.set_effect(settings.effect)
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
        # The rhythm/structure models are about to see a discontinuity (new
        # track or seek); clear them so the grid re-locks cleanly.
        self._tempo.reset()
        self._structure.reset()
        self._beat_anchor = None  # downbeat ref is stale after a discontinuity

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

                # Refresh the card-facing attributes (~1 Hz; only writes HA state
                # when something actually changed). Runs in every branch below.
                if now - self._last_publish >= 1.0:
                    self._last_publish = now
                    self._maybe_publish()

                try:
                    if self._source is None:
                        if now - last_reopen >= _IDLE_REOPEN_S:
                            last_reopen = now
                            await self._ensure_source()
                        if self._source is None:
                            idle_color_phase += dt * 0.05
                            await self._send_idle(idle_color_phase)
                            await asyncio.sleep(1.0 / _IDLE_FPS)
                            continue

                    # Paused/stopped: idle (keep the source for a fast resume).
                    pstate = self._hass.states.get(self._source.entity_id)
                    if pstate is None or pstate.state != "playing":
                        idle_color_phase += dt * 0.05
                        await self._send_idle(idle_color_phase)
                        await asyncio.sleep(1.0 / _IDLE_FPS)
                        continue

                    frame = await self._source.read_frame()  # paced to real time
                    if frame is None:
                        await self._reset_source()
                        continue

                    self._maybe_refresh_album_art()
                    # Predictive beat grid + structure drive anticipation and
                    # build/drop choreography. (A decode-ahead source can later
                    # pass a real future slice as the structure lookahead.)
                    beatgrid = self._tempo.update(
                        frame.t_audio, frame.flux, frame.beat, frame.beat_strength
                    )
                    self._last_beatgrid = beatgrid
                    structure = self._structure.update(frame)
                    colors = self._engine.render(frame, period, beatgrid, structure)
                    await self._send_timed(colors)
                except ConnectionError:
                    # DTLS channel dropped: try to recover instead of ending sync.
                    if not await self._reconnect_stream():
                        _LOGGER.warning(
                            "DTLS channel lost for %s; giving up after %d retries",
                            self._config.name, _RECONNECT_ATTEMPTS,
                        )
                        self._running = False
                    last_t = time.monotonic()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Music sync loop crashed for %s", self._config.name)
        finally:
            # If the loop ended on its own (DTLS gave up, crash) rather than via
            # stop(), hand the area back to the bridge so it restores the prior
            # light state immediately, instead of leaving the lamps frozen on the
            # last frame until the bridge times the stream out (~10 s).
            if not self._stopping:
                await self._safe_release_stream()
                if self._on_finished is not None:
                    self._on_finished()

    async def _safe_release_stream(self) -> None:
        """Best-effort teardown of a self-ended session (never raises)."""
        try:
            await self._stream.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._bridge.stop_stream(self._config.id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Release of %s stream failed: %s", self._config.name, err)
        await self._restore_snapshot()

    async def _send_idle(self, phase: float) -> None:
        # Gentle dim glow drifting through the palette (paused / waiting for audio).
        await self._safe_send(self._engine.render_idle(phase))

    async def _send_timed(self, colors: dict) -> None:
        """Send the frame through the timing-offset delay buffer.

        Effective delay = TIMING_BUFFER_MS + the user's offset (can be negative
        down to zero). Positive = lights later; negative = earlier (within the
        baseline buffer). Lets the user align lights to the sound.
        """
        delay_s = max(0.0, (TIMING_BUFFER_MS + self._settings.timing_ms) / 1000.0)
        if delay_s <= 0.001:
            self._delay_buf.clear()
            await self._safe_send(colors)
            return
        now = time.monotonic()
        self._delay_buf.append((now, colors))
        target = now - delay_s
        send = None
        while self._delay_buf and self._delay_buf[0][0] <= target:
            send = self._delay_buf.popleft()[1]
        if send is not None:
            await self._safe_send(send)  # else still filling the buffer; hold

    async def _safe_send(self, colors: dict[int, tuple[float, float, float]]) -> None:
        # ConnectionError propagates to the run loop, which attempts a reconnect.
        now = time.monotonic()
        dt = 0.025 if self._last_safe_t is None else max(0.0, now - self._last_safe_t)
        self._last_safe_t = now
        colors = self._safety.process(colors, dt)
        # Split large areas across packets (the bridge caps a packet at ~10 lights).
        for frame in self._encoder.build_packets(colors):
            await self._stream.send(frame)

    async def _reconnect_stream(self) -> bool:
        """Re-establish a dropped DTLS channel with backoff; True on success."""
        try:
            await self._stream.stop()
        except Exception:  # noqa: BLE001 - best-effort teardown before retrying
            pass
        for attempt in range(1, _RECONNECT_ATTEMPTS + 1):
            if not self._running or self._stopping:
                return False
            delay = min(_RECONNECT_BASE_S * attempt, _RECONNECT_MAX_S)
            await asyncio.sleep(delay)
            try:
                await self._bridge.start_stream(self._config.id)
                await self._stream.start()
            except Exception as err:  # noqa: BLE001 - keep retrying on any failure
                _LOGGER.info(
                    "Reconnect attempt %d/%d for %s failed: %s",
                    attempt, _RECONNECT_ATTEMPTS, self._config.name, err,
                )
                continue
            self._delay_buf.clear()  # drop stale frames buffered before the drop
            self._safety.reset()  # field history is stale after the gap
            self._last_safe_t = None
            _LOGGER.info("Reconnected DTLS stream for %s", self._config.name)
            return True
        return False

    def _maybe_refresh_album_art(self) -> None:
        # Only when the Album colour is selected; preset themes are static.
        if self._settings.colour != ColorScheme.ALBUM_ART or self._source is None:
            return
        track = self._source.track_id
        if track == self._last_track:
            return
        # A previous extraction is still running: retry next loop. Crucially we do
        # NOT mark this track handled yet, or a transient state would skip it.
        if self._art_task and not self._art_task.done():
            return
        url = self._source.album_art_url
        if not url:
            return  # artwork URL not populated for the new track yet; retry later
        # Only now claim the track as handled, since we're actually extracting it.
        self._last_track = track
        self._art_task = self._hass.async_create_task(self._extract_art(url))

    async def _extract_art(self, url: str) -> None:
        palette = await extract_palette(self._ffmpeg, url)
        if palette is not None and self._settings.colour == ColorScheme.ALBUM_ART:
            self._engine.set_palette(palette)
            self._album_hex = self._palette_to_hex(palette)
            self._maybe_publish()  # surface the new album colours to cards at once
            _LOGGER.info(
                "Album colours for %s: %s",
                self._config.name,
                [tuple(round(v, 2) for v in c) for c in palette.colors],
            )
        elif palette is None:
            _LOGGER.warning("Album-art extraction failed for %s (%s)", self._config.name, url)

    @staticmethod
    def _palette_to_hex(palette) -> list[str]:
        """Render a Palette's anchor colours as ``#rrggbb`` strings for the UI."""
        out: list[str] = []
        for c in palette.colors:
            r, g, b = (int(round(max(0.0, min(1.0, x)) * 255)) for x in c)
            out.append(f"#{r:02x}{g:02x}{b:02x}")
        return out

    def _compute_public_state(self) -> dict:
        """Now-playing / album-colour / tempo data exposed on the switch entity.

        Lets a dashboard card recolour itself to the extracted album palette and
        lock a visualizer to the song (position from the player, tempo here).
        """
        state: dict = {}
        if self._album_hex:
            state["album_colors"] = self._album_hex
        bg = self._last_beatgrid
        if bg is not None and bg.locked and bg.bpm > 0:
            state["bpm"] = round(bg.bpm)
        entity_id = self._source.entity_id if self._source is not None else None
        if entity_id:
            ps = self._hass.states.get(entity_id)
            if ps is not None:
                a = ps.attributes
                if a.get("media_title"):
                    state["media_title"] = a["media_title"]
                if a.get("media_artist"):
                    state["media_artist"] = a["media_artist"]
                # Deliberately NOT `entity_picture` (a reserved attribute that
                # would replace the switch's own icon in the UI); the card reads
                # `media_image`.
                if a.get("entity_picture"):
                    state["media_image"] = a["entity_picture"]
                # Playback position anchors so a card's visualizer can run a beat
                # grid locked to the song. These re-anchor only on seek / play-
                # pause / track-change, so mirroring them doesn't spam the recorder
                # (the card extrapolates the live position between updates).
                if a.get("media_position") is not None:
                    state["media_position"] = a["media_position"]
                if a.get("media_position_updated_at") is not None:
                    state["media_position_updated_at"] = a["media_position_updated_at"]
                anchor = self._beat_anchor_on_timeline(bg, a)
                if anchor is not None:
                    state["beat_anchor"] = anchor
                state["source_player"] = entity_id
        return state

    def _beat_anchor_on_timeline(self, bg: BeatGrid | None, attrs) -> float | None:
        """A recent detected-beat position (s) on the player's media timeline.

        Lets a card lock its beat grid to the real downbeats instead of assuming
        beat 0 sits at position 0. Folded into ``[0, beat_period)`` and held stable
        (re-published only when it shifts meaningfully), so steady tempo doesn't
        churn the recorder.
        """
        if bg is None or not bg.locked or bg.bpm <= 0:
            return None
        pos = attrs.get("media_position")
        if pos is None:
            return None
        period = 60.0 / bg.bpm
        live = float(pos)
        updated = attrs.get("media_position_updated_at")
        if updated is not None:
            try:
                live += max(0.0, (dt_util.utcnow() - updated).total_seconds())
            except (TypeError, ValueError):
                pass
        # Position of the most recent beat, folded to one beat period.
        anchor = (live - bg.phase * period) % period
        prev = self._beat_anchor
        if prev is None or _circular_distance(anchor, prev, period) > 0.04:
            self._beat_anchor = round(anchor, 3)
        return self._beat_anchor

    def _maybe_publish(self) -> None:
        """Update the exposed attributes, but only fire a state write when they
        actually change — keeps the state machine and recorder quiet."""
        state = self._compute_public_state()
        if state == self.public_state:
            return
        self.public_state = state
        async_dispatcher_send(self._hass, signal_area_update(self._config.id))

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
        await self._restore_snapshot()

    async def _restore_snapshot(self) -> None:
        """Re-apply the captured pre-sync light state (opt-in; never raises)."""
        if not self._restore_lights or not self._light_snapshot:
            return
        # Let the bridge's own restore-on-stop settle first, then re-apply our
        # snapshot to fix any light it dropped.
        await asyncio.sleep(0.4)
        try:
            await self._bridge.restore_light_states(self._light_snapshot)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Light restore for %s failed: %s", self._config.name, err)
        finally:
            self._light_snapshot = None


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
        self._restore_lights: bool = bool(
            entry.options.get(CONF_RESTORE_LIGHTS, DEFAULT_RESTORE_LIGHTS)
        )
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

    def area_attributes(self, area_id: str) -> dict:
        """Now-playing / album-colour / bpm data for an active area (for cards).

        Empty when the area isn't syncing, so the switch drops the attributes.
        """
        session = self._sessions.get(area_id)
        return dict(session.public_state) if session is not None else {}

    async def update_settings(self, area_id: str, **changes) -> None:
        current = self.get_settings(area_id)
        updated = replace(current, **changes)
        self._settings[area_id] = updated
        if (session := self._sessions.get(area_id)) is not None:
            session.apply_settings(updated)
        await self._persist_settings()

    def _all_managers(self) -> list[SyncManager]:
        """Every SyncManager registered across the integration (all bridges)."""
        data = self.hass.data.get(DOMAIN, {})
        return [m for m in data.values() if isinstance(m, SyncManager)]

    async def _enforce_single_active_area(self, keep_area_id: str) -> None:
        """Stop every other active area so only one streams at a time.

        A Hue bridge supports a single entertainment stream, and we extend that
        to a hard guarantee across the whole integration: starting any area first
        deactivates every other active area (on this or any other bridge).
        """
        for manager in self._all_managers():
            for other_id in list(manager._sessions):
                if manager is self and other_id == keep_area_id:
                    continue
                _LOGGER.info(
                    "Stopping area %s so %s can take the single active stream",
                    other_id, keep_area_id,
                )
                await manager.stop_area(other_id)

    async def start_area(self, area_id: str) -> None:
        if area_id in self._sessions:
            return
        config = self.configs.get(area_id)
        if config is None:
            raise ValueError(f"Unknown entertainment area {area_id}")
        # Only one entertainment area may be active at a time, anywhere.
        await self._enforce_single_active_area(area_id)
        # Refresh channels/status in case the area changed in the Hue app.
        config = await self.bridge.get_entertainment_config(area_id)
        self.configs[area_id] = config
        session = SyncSession(
            self.hass, self.bridge, self._host, self._app_key, self._client_key,
            self._ffmpeg, config, self.get_settings(area_id),
            snapserver_host=self._snapserver_host,
            restore_lights=self._restore_lights,
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
