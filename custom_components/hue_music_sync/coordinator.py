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

from .audio.map_source import TrackMapSource
from .audio.metadata import MetadataSource
from .audio.ma_stream import is_snapcast_backed
from .audio.snapcast import SnapcastSource
from .audio.source import (
    MusicAssistantSource,
    ma_player_provider,
    resolve_map_url,
    resolve_next_map,
)
from .audio.structure import StructureTracker
from .audio.tempo import BeatGrid, TempoTracker
from .audio.trackmap import Section, TrackMapper
from .color.album_art import extract_palette
from .color.palette import Palette
from .const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    BANDS,
    CONF_AREAS,
    CONF_BRIGHTNESS,
    CONF_COLOUR,
    CONF_EFFECT,
    CONF_LATENCY_MS,
    CONF_MEDIA_PLAYER,
    CONF_MODE,
    CONF_RESTORE_LIGHTS,
    CONF_SNAPSERVER_HOST,
    CONF_SUBSONIC_PASSWORD,
    CONF_SUBSONIC_URL,
    CONF_SUBSONIC_USER,
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
    LIGHT_PIPELINE_MS,
    TIMING_BUFFER_MS,
    ColorScheme,
    SyncEffect,
    SyncMode,
    signal_area_update,
)
from .effects.engine import EffectEngine
from .effects.modes import UNRESTRAINED_MODES
from .effects.safety import FieldSafety
from .hue.bridge import EntertainmentConfig, HueBridge
from .hue.stream import DtlsStream, HueStreamEncoder

_LOGGER = logging.getLogger(__name__)

_IDLE_FPS = 10
_IDLE_REOPEN_S = 2.0

# DTLS auto-reconnect: a dropped channel (transient packet loss, bridge hiccup)
# should recover on its own instead of silently ending the session. The window
# must outlast a full bridge reboot / Wi-Fi AP restart (~60-90 s): with 14
# attempts at 1,2,…,10,10,… s of backoff this keeps trying for ~95 s before
# giving the area back, instead of quietly ending the show after ~15 s.
_RECONNECT_ATTEMPTS = 14
_RECONNECT_BASE_S = 1.0
_RECONNECT_MAX_S = 10.0

# Background upgrade of the metadata fallback to a real tap: probe soon after
# falling back (the MA stream / queue streamdetails are usually ready within a
# second or two — especially for a single ad-hoc track), then back off only
# mildly so a song that starts on metadata still recovers to track-map playback
# within a few seconds instead of being stranded on the non-reactive fallback.
_META_UPGRADE_START_S = 1.5
_META_UPGRADE_MAX_S = 6.0

# Drum-pad mode auto-expires this long after the last tap / keepalive, so an
# abruptly-closed card can never strand the room with its automatic beats off.
_DRUM_WINDOW_S = 4.0

# One regime per track (item 4 — "pick one and stick with it"): the offline map
# grid is adopted only if it's ready within this many seconds of the track's
# start (cached / pre-warmed / quick analysis). If the track is already well
# underway on the causal grid, we stay on it for the rest of the track rather
# than switching mid-song, and the freshly-analysed map is cached for next time.
_MAP_COMMIT_WINDOW_S = 6.0


def trackmap_cache_dir(hass) -> str:
    """The shared on-disk track-map cache directory (coordinator + pre-warm)."""
    return hass.config.path("hue_music_sync", "trackmaps")


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
        subsonic=None,
        on_finished: Callable[[], None] | None = None,
        ws_broadcast: Callable[[dict], None] | None = None,
        ws_active: Callable[[], bool] | None = None,
    ) -> None:
        self._hass = hass
        self._bridge = bridge
        self._ffmpeg = ffmpeg_bin
        self._subsonic = subsonic  # (url, user, password) for OpenSubsonic, or None
        self._config = config
        self._settings = settings
        self._snapserver_host = snapserver_host
        self._restore_lights = restore_lights
        self._light_snapshot: list[dict] | None = None
        self._on_finished = on_finished
        # Live card feed: only built/sent while at least one card listens.
        self._ws_broadcast = ws_broadcast
        self._ws_active = ws_active or (lambda: False)
        self._ws_last = 0.0

        self._encoder = HueStreamEncoder(config.id)
        self._stream = DtlsStream(host, app_key, client_key)
        self._engine = EffectEngine(config.channels)
        # Final safety stage (whole-field flash limiter + red guard). Applied to
        # every emitted frame in _safe_send EXCEPT in the explicitly
        # unrestrained club modes (Intense/Extreme with a non-Movies effect),
        # which the user opts into knowing they flash as hard as Hue allows.
        self._safety = FieldSafety()
        self._was_unrestrained = False
        self._last_safe_t: float | None = None
        # Predictive beat grid + musical-structure trackers, fed the analyzer's
        # feature stream so the engine can anticipate beats and ride builds/drops.
        _period = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE
        self._tempo = TempoTracker(_period)
        self._structure = StructureTracker(_period)
        # Offline track maps (Hue×Spotify-style scheduled beats): analysed in the
        # background per track, then queried by playback position each frame.
        self._mapper = TrackMapper(
            ffmpeg_bin,
            spawner=lambda coro, name: hass.async_create_background_task(coro, name),
            # Persist analyzed maps under HA's config dir so a track plays
            # instantly the second time (and after a library pre-warm).
            cache_dir=trackmap_cache_dir(hass),
        )
        self._map_track: str | None = None  # last track a map URL was resolved for
        self._map_check = 0.0
        self._map_prev_pos: float | None = None
        self._map_section: Section | None = None
        # Per-track regime commitment (item 4): None = undecided, True = use the
        # offline map grid this track, False = stay on the causal grid this track.
        self._map_commit: bool | None = None
        self._play_track: str | None = None  # last track seen by the rhythm models
        self._fallback_track: str | None = None  # track the metadata source opened on
        self._source: (
            MusicAssistantSource | MetadataSource | SnapcastSource | TrackMapSource | None
        ) = None
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
        # Background probe that upgrades the metadata fallback to a real tap.
        self._upgrade_task: asyncio.Task | None = None
        self._upgrade_at = 0.0
        self._upgrade_interval = _META_UPGRADE_START_S
        self._prefetch_key: str | None = None  # next-track map already requested
        self._drum_until = 0.0  # drum-pad mode active while monotonic() < this

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
        if settings.mode != prev.mode or settings.effect != prev.effect:
            # The flash limiter holds a slow brightness anchor and freezes it
            # while it's limiting, so a mode/effect change carries the *previous*
            # mode's field level forward: switching e.g. Subtle (steady ~0.8) to a
            # dark club mode would pin the room near the old bright anchor and
            # suppress the new mode's flashing until the session was restarted.
            # Clear it so the new mode anchors to its own field from this frame.
            self._safety.reset()
            self._was_unrestrained = self._unrestrained()
            self._last_safe_t = None
        if settings.colour != prev.colour:
            self._apply_colour()
            self._last_track = None  # re-extract album art if switching to Album
            self._map_section = None  # re-apply Song section palette on next poll
        if self._source is not None and settings.media_player != prev.media_player:
            # Player changed: drop current source so the loop re-opens it.
            self._hass.async_create_task(self._reset_source())

    # -- drum-pad (manual beats) ----------------------------------------------

    def set_drum_mode(self, active: bool) -> None:
        """Arm/refresh (or release) drum-pad mode from the card.

        Auto-expires (see ``_DRUM_WINDOW_S``); the card keepalives it while the
        page is open, so a missed close can't leave the room's auto beats off.
        """
        self._drum_until = time.monotonic() + _DRUM_WINDOW_S if active else 0.0

    def manual_tap(self, group: str, strength: float = 0.95) -> None:
        """A pad tap from the card: flash the group's lights and keep drum mode on."""
        self._drum_until = time.monotonic() + _DRUM_WINDOW_S
        self._engine.manual_flash(group, strength)

    def _apply_colour(self) -> None:
        # Preset themes are static palettes. Album art and Song are dynamic
        # (extracted per track / per section), and keep the engine fallback
        # palette until their colours are ready.
        if self._settings.colour not in (ColorScheme.ALBUM_ART, ColorScheme.SONG):
            self._engine.set_scheme(self._settings.colour)

    def _reset_rhythm_models(self) -> None:
        """Clear the tempo/structure/track state for a clean re-lock.

        Used whenever the audio timeline jumps under us — a track change, a
        source reset, or swapping the metadata fallback for a real tap — so the
        previous track's locked grid can't keep firing stale beats on the new one.
        """
        self._last_track = None
        self._play_track = None
        self._tempo.reset()
        self._structure.reset()
        self._beat_anchor = None  # downbeat ref is stale after a discontinuity
        self._map_track = None
        self._map_prev_pos = None
        self._map_section = None
        self._prefetch_key = None  # re-evaluate the next track after a jump

    async def _reset_source(self) -> None:
        await self._cancel_meta_upgrade()
        if self._source is not None:
            await self._source.close()
            self._source = None
        self._reset_rhythm_models()

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

    async def _try_open(self, src):
        """Open one candidate source; return it if it works, else close it.

        Cancellation (a background upgrade probe being torn down) closes the
        half-opened source before propagating, so a probed ffmpeg never leaks.
        """
        try:
            if await src.open():
                return src
        except asyncio.CancelledError:
            await src.close()
            raise
        except Exception:  # noqa: BLE001 - a bad candidate must not abort acquisition
            _LOGGER.debug(
                "Source %s failed to open", type(src).__name__, exc_info=True
            )
        await src.close()
        return None

    async def _open_real_source(self):
        """Open the best *real-audio* source, or None if none is tappable yet.

        The real-audio ladder (everything except the generic metadata fallback):
          1. Snapcast tap — real, beat-accurate audio off the snapserver. Gated
             on the player's MA provider so a Sendspin/squeezelite/cast session
             can't latch onto another room's snapcast stream.
          2. Music Assistant HTTP tap (any player exposing a stream).
          3. Offline track-map playback (AirPlay/Cast/Sonos/DLNA/... that report
             a position and a resolvable per-track URL).

        Pulled out of :meth:`_ensure_source` so it can also run as a background
        probe that upgrades a metadata fallback the moment a real tap appears.
        """
        entity_id = self._resolve_player()
        if entity_id is None:
            return None
        if self._snapserver_host and is_snapcast_backed(
            ma_player_provider(self._hass, entity_id)
        ):
            entry = er.async_get(self._hass).async_get(entity_id)
            player_uid = entry.unique_id if entry else None
            snap = await self._try_open(SnapcastSource(
                self._hass, entity_id, self._snapserver_host, self._ffmpeg, player_uid
            ))
            if snap is not None:
                return snap
        ma = await self._try_open(MusicAssistantSource(
            self._hass, entity_id, self._ffmpeg, self._settings.latency_ms
        ))
        if ma is not None:
            return ma
        return await self._try_open(
            TrackMapSource(self._hass, entity_id, self._mapper, self._subsonic)
        )

    async def _ensure_source(self) -> bool:
        if self._source is not None:
            return True
        real = await self._open_real_source()
        if real is not None:
            self._source = real
            return True

        # Metadata-driven animation (universal fallback). It has no real audio,
        # so schedule a background probe to upgrade to a real tap the moment one
        # becomes available — at the start of a song (or after a long idle) the
        # MA stream session is often not ready for a second or two, and without
        # this the show would stay on the generic animation until the next track
        # change. Remember the track it opened on so a track change also re-evals.
        entity_id = self._resolve_player()
        if entity_id is None:
            return False
        meta = MetadataSource(self._hass, entity_id)
        if await meta.open():
            _LOGGER.info(
                "No tappable audio for %s yet; using metadata-driven sync and "
                "probing for a real tap", entity_id
            )
            self._source = meta
            self._fallback_track = meta.track_id
            self._schedule_meta_upgrade()
            return True
        return False

    def _schedule_meta_upgrade(self) -> None:
        """(Re)arm the background probe that upgrades the metadata fallback."""
        self._upgrade_interval = _META_UPGRADE_START_S
        self._upgrade_at = time.monotonic() + self._upgrade_interval

    async def _maybe_upgrade_source(self, now: float) -> None:
        """Swap a metadata fallback for a real tap once a probe finds one.

        Runs at ~1 Hz from the playing path. The probe itself runs as a
        background task so the render loop never stalls on an ffmpeg open; on
        success we hand the show over to the real source and reset the rhythm
        models for a clean lock. Probe cadence backs off so a genuinely
        tap-less player settles to an occasional check instead of spinning.
        """
        task = self._upgrade_task
        if task is not None and task.done():
            self._upgrade_task = None
            better = None
            try:
                better = task.result()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Source upgrade probe failed", exc_info=True)
            if better is not None:
                if isinstance(self._source, MetadataSource):
                    _LOGGER.info(
                        "Upgraded %s from metadata to %s",
                        self._config.name, type(better).__name__,
                    )
                    old = self._source
                    self._source = better
                    self._reset_rhythm_models()
                    await old.close()
                else:
                    await better.close()  # source already changed; discard probe
        if (
            isinstance(self._source, MetadataSource)
            and self._upgrade_task is None
            and now >= self._upgrade_at
        ):
            self._upgrade_interval = min(_META_UPGRADE_MAX_S, self._upgrade_interval * 2)
            self._upgrade_at = now + self._upgrade_interval
            self._upgrade_task = self._hass.async_create_background_task(
                self._open_real_source(), f"hue_music_sync_upgrade_{self._config.id}"
            )

    async def _cancel_meta_upgrade(self) -> None:
        """Tear down any in-flight upgrade probe (on stop / source reset)."""
        task = self._upgrade_task
        self._upgrade_task = None
        if task is None:
            return
        task.cancel()
        try:
            result = await task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - probe teardown errors are non-fatal
            _LOGGER.debug("Upgrade probe teardown error", exc_info=True)
            return
        # The probe had already finished before we cancelled it: close any
        # real source it produced so its decoder doesn't leak.
        if result is not None:
            try:
                await result.close()
            except Exception:  # noqa: BLE001
                pass

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
                # Never let a publish error take down the sync loop.
                if now - self._last_publish >= 1.0:
                    self._last_publish = now
                    try:
                        self._maybe_publish()
                        if self._ws_broadcast is not None and self._ws_active():
                            self._ws_broadcast(self.ws_meta())
                    except Exception:  # noqa: BLE001
                        _LOGGER.debug(
                            "Card attribute publish failed for %s",
                            self._config.name, exc_info=True,
                        )

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
                    if now - self._map_check >= 1.0:
                        self._map_check = now
                        self._maybe_track_map()
                        self._maybe_prefetch_next()
                        # A metadata fallback shouldn't be a life sentence: on a
                        # track change, drop it and re-evaluate the better
                        # sources (the next track may be tappable/analysable).
                        if (
                            isinstance(self._source, MetadataSource)
                            and self._source.track_id != self._fallback_track
                        ):
                            await self._reset_source()
                            continue
                        # …and even on the *same* track, keep probing in the
                        # background for a real tap and swap it in once ready, so
                        # a transient at the start of a song (or after a long
                        # idle) doesn't strand the show on the generic animation.
                        # This frame still renders; the new source reads next loop.
                        await self._maybe_upgrade_source(now)
                    # Track change: reset the rhythm/structure models so the
                    # previous song's locked tempo can't keep firing stale beats
                    # on the new one (which read as flashes that don't match the
                    # song). The map state is reset alongside so the new track's
                    # grid replaces it cleanly once analysed.
                    tid = self._source.track_id
                    if tid and tid != self._play_track:
                        self._play_track = tid
                        self._tempo.reset()
                        self._structure.reset()
                        self._beat_anchor = None
                        self._map_prev_pos = None
                        self._map_section = None
                        self._map_commit = None  # re-decide the regime per track
                    # Causal beat grid (always running; the fallback rhythm model).
                    beatgrid = self._tempo.update(
                        frame.t_audio, frame.flux, frame.beat, frame.beat_strength,
                        bass=max(
                            frame.bands.get("sub_bass", 0.0),
                            frame.bands.get("bass", 0.0),
                        ),
                    )
                    structure = self._structure.update(frame)
                    # The offline track map, when ready, supplies the *authoritative*
                    # grid (exact scheduled beats, real downbeats) and the section
                    # arc — the live analyzer keeps supplying the actual energies.
                    map_grid = self._apply_track_map(structure)
                    if map_grid is not None:
                        beatgrid = map_grid
                    self._last_beatgrid = beatgrid
                    # Drum-pad mode (card taps drive the beats) auto-expires, so
                    # set it from the live window every frame before rendering.
                    self._engine.set_manual_only(now < self._drum_until)
                    colors = self._engine.render(frame, period, beatgrid, structure)
                    features = (
                        self._ws_features(frame) if self._ws_active() else None
                    )
                    await self._send_timed(colors, features)
                except ConnectionError:
                    # DTLS channel dropped: try to recover instead of ending sync.
                    if not await self._reconnect_stream():
                        _LOGGER.warning(
                            "DTLS channel lost for %s; giving up after %d retries",
                            self._config.name, _RECONNECT_ATTEMPTS,
                        )
                        self._running = False
                    last_t = time.monotonic()
                except Exception:  # noqa: BLE001
                    # A transient fault in the frame path — most often a hiccup at
                    # a track boundary (the stream/decoder/MA lookup raising as one
                    # song ends and the next begins) — must NOT tear the session
                    # down and force a manual switch toggle. Drop the source so the
                    # loop cleanly re-acquires the next track, and pace the retry so
                    # a persistent fault can't spin the loop hot.
                    _LOGGER.exception(
                        "Recovering sync loop for %s after an unexpected error",
                        self._config.name,
                    )
                    try:
                        await self._reset_source()
                    except Exception:  # noqa: BLE001 - never fail the recovery
                        self._source = None
                    await asyncio.sleep(0.5)
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

    async def _send_timed(self, colors: dict, features: dict | None = None) -> None:
        """Send the frame through the timing-offset delay buffer.

        The baseline delay aligns the lights with the *audible* sound: a source
        whose analysis runs ahead of the speakers (snapcast decodes chunks
        ``bufferMs`` before clients play them) reports that lead, and we hold
        frames for the lead minus the light pipeline's own latency. Sources
        already position-locked to playback use the small fixed buffer instead.
        The user's timing offset remains a fine trim on top (positive = lights
        later; negative = earlier, within the baseline buffer). ``features``
        (the live-card payload for this frame) rides the same buffer so the
        card's visualizer matches what the room is showing/hearing.
        """
        # Drum-pad mode: the user is driving the beats, so audio alignment is
        # irrelevant — skip the delay buffer entirely so taps reach the bulbs
        # with the least possible latency.
        if self._engine.manual_only:
            self._delay_buf.clear()
            await self._safe_send(colors, features)
            return
        lead_ms = getattr(self._source, "playback_lead_ms", 0) or 0
        base_ms = max(0, lead_ms - LIGHT_PIPELINE_MS) if lead_ms > 0 else TIMING_BUFFER_MS
        delay_s = max(0.0, (base_ms + self._settings.timing_ms) / 1000.0)
        if delay_s <= 0.001:
            self._delay_buf.clear()
            await self._safe_send(colors, features)
            return
        now = time.monotonic()
        self._delay_buf.append((now, (colors, features)))
        target = now - delay_s
        send = None
        while self._delay_buf and self._delay_buf[0][0] <= target:
            send = self._delay_buf.popleft()[1]
        if send is not None:
            await self._safe_send(*send)  # else still filling the buffer; hold

    def _unrestrained(self) -> bool:
        """True in the explicit club modes that bypass the flash limiter."""
        return (
            self._settings.mode in UNRESTRAINED_MODES
            and self._settings.effect is not SyncEffect.MOVIES
        )

    async def _safe_send(
        self,
        colors: dict[int, tuple[float, float, float]],
        features: dict | None = None,
    ) -> None:
        # ConnectionError propagates to the run loop, which attempts a reconnect.
        now = time.monotonic()
        dt = 0.025 if self._last_safe_t is None else max(0.0, now - self._last_safe_t)
        self._last_safe_t = now
        unrestrained = self._unrestrained()
        if not unrestrained:
            if self._was_unrestrained:
                self._safety.reset()  # field history is stale after a bypass
            colors = self._safety.process(colors, dt)
        self._was_unrestrained = unrestrained
        # Split large areas across packets (the bridge caps a packet at ~10 lights).
        for frame in self._encoder.build_packets(colors):
            await self._stream.send(frame)
        self._ws_stream(colors, features)

    # -- live card feed --------------------------------------------------------

    @staticmethod
    def _ws_features(frame) -> dict:
        """The per-frame analysis the card's live visualizer runs on."""
        return {
            "bands": [round(frame.bands.get(n, 0.0), 3) for n in BANDS],
            "energy": round(frame.energy, 3),
            "beat": bool(frame.bass_beat),
            "strength": round(frame.bass_strength, 2),
        }

    def _ws_stream(self, colors: dict, features: dict | None) -> None:
        """Broadcast the emitted frame to subscribed cards (~20 Hz, throttled)."""
        if self._ws_broadcast is None or not self._ws_active():
            return
        now = time.monotonic()
        if now - self._ws_last < 0.05:
            return
        self._ws_last = now
        payload: dict = {
            "type": "stream",
            "lights": {
                str(cid): "#%02x%02x%02x"
                % tuple(int(max(0.0, min(1.0, v)) * 255) for v in rgb)
                for cid, rgb in colors.items()
            },
            "roles": {str(c): r for c, r in self._engine.roles.items()},
        }
        if features:
            payload.update(features)
        self._ws_broadcast(payload)

    def ws_meta(self) -> dict:
        """The slow picture (~1 Hz): lamp layout, sections, tempo, position."""
        payload: dict = {"type": "meta"}
        payload["positions"] = {
            str(cid): [round(info["nx"], 3), round(info["ny"], 3), round(info["nz"], 3)]
            for cid, info in self._engine.cmap.items()
        }
        bg = self._last_beatgrid
        if bg is not None and bg.locked and bg.bpm > 0:
            payload["bpm"] = round(bg.bpm, 1)
        src = self._source
        if src is not None:
            tm = self._mapper.get(src.track_id)
            if tm is not None:
                payload["sections"] = [
                    [round(s.start, 1), round(s.end, 1), round(s.energy, 2)]
                    for s in tm.sections
                ]
                payload["duration"] = round(tm.duration, 1)
            state = self._hass.states.get(src.entity_id)
            if state is not None:
                payload["playing"] = state.state == "playing"
                pos = state.attributes.get("media_position")
                if pos is not None:
                    live = float(pos)
                    updated = state.attributes.get("media_position_updated_at")
                    if state.state == "playing" and updated is not None:
                        try:
                            live += max(
                                0.0, (dt_util.utcnow() - updated).total_seconds()
                            )
                        except (TypeError, ValueError):
                            pass
                    payload["position"] = round(live, 2)
                duration = state.attributes.get("media_duration")
                if duration:
                    payload["duration"] = float(duration)
        return payload

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

    def _maybe_track_map(self) -> None:
        """Kick off offline analysis of the current track (once per track)."""
        src = self._source
        if src is None:
            return
        track = src.track_id
        if not track or track == self._map_track:
            return
        url = resolve_map_url(self._hass, src.entity_id, self._subsonic)
        if url is None:
            return  # nothing tappable per-track (radio/flow); retried next poll
        self._map_track = track
        self._map_prev_pos = None
        self._map_section = None
        self._mapper.ensure(track, url)

    def _maybe_prefetch_next(self) -> None:
        """Pre-analyse the *next* queue item so a gapless change is instant.

        Only once the current track's own map is ready (so prefetch never delays
        the track that's actually playing — the mapper analyses one at a time),
        and keyed to match whichever scheme the active source will use when that
        item becomes current (the live tap keys by uri, the track-map by the full
        signature), so the warmed map is found on the change instead of a gap.
        """
        src = self._source
        if src is None or isinstance(src, MetadataSource):
            return
        if self._mapper.get(src.track_id) is None:
            return  # current track not analysed yet; don't compete for the slot
        nxt = resolve_next_map(self._hass, src.entity_id, self._subsonic)
        if nxt is None:
            return
        next_uri, next_sig, next_url = nxt
        key = next_sig if isinstance(src, TrackMapSource) else next_uri
        if key == self._prefetch_key:
            return  # already requested this next track
        self._prefetch_key = key
        _LOGGER.debug(
            "Prefetching next-track map for %s (%s)", self._config.name, key
        )
        self._mapper.ensure(key, next_url)

    def _analysis_position(self) -> float | None:
        """The track position our *analysis frames* currently correspond to.

        A source that tracks its own playhead (track-map playback) reports it
        directly; otherwise it's the live playhead from the player plus the
        source's analysis lead (snapcast decodes chunks ``bufferMs`` before
        the speakers play them).
        """
        src = self._source
        if src is None:
            return None
        own = getattr(src, "analysis_position", None)
        if own is not None:
            return float(own)
        state = self._hass.states.get(src.entity_id)
        if state is None:
            return None
        pos = state.attributes.get("media_position")
        if pos is None:
            return None
        live = float(pos)
        updated = state.attributes.get("media_position_updated_at")
        if state.state == "playing" and updated is not None:
            try:
                live += max(0.0, (dt_util.utcnow() - updated).total_seconds())
            except (TypeError, ValueError):
                pass
        lead_ms = getattr(src, "playback_lead_ms", 0) or 0
        return live + lead_ms / 1000.0

    def _apply_track_map(self, structure) -> BeatGrid | None:
        """Query the track map at the current position; enrich ``structure``.

        Returns the scheduled beat grid (or None to stay on the causal one) and
        fills in the section level / predictive drop flags from the map.
        """
        src = self._source
        if src is None:
            return None
        tm = self._mapper.get(src.track_id)
        pos = self._analysis_position()
        # The offline TrackMapSource *is* the map regime — always use its map.
        # For a live tap, commit to one regime per track (item 4): adopt the map
        # only if it's ready near the track start, else stay causal for the rest
        # of the track rather than switching the grid mid-song. Decided once.
        if isinstance(src, TrackMapSource):
            use_map = tm is not None
        else:
            if self._map_commit is None and pos is not None:
                if tm is not None and pos <= _MAP_COMMIT_WINDOW_S:
                    self._map_commit = True
                elif pos > _MAP_COMMIT_WINDOW_S:
                    self._map_commit = False
            use_map = bool(self._map_commit)
        if not use_map or tm is None or pos is None:
            return None
        grid = tm.grid_at(pos, self._map_prev_pos)
        self._map_prev_pos = pos

        section = tm.section_at(pos)
        prev = self._map_section
        if section is not None:
            structure.section_level = section.energy
            # A boundary into a clearly louder section is a *scheduled* drop.
            boundary = tm.next_boundary(pos)
            if (
                boundary is not None
                and boundary[0] <= 1.5
                and boundary[1] > section.energy + 0.15
            ):
                structure.drop_imminent = True
            if (
                prev is not None
                and section is not prev
                and section.energy > prev.energy + 0.15
            ):
                structure.drop_now = True
            # Song colour scheme: recolour the room to this section's harmony
            # (chroma -> hue) as each section arrives, via the same palette
            # setter album art uses. Publish it so the dashboard card themes too.
            if (
                self._settings.colour == ColorScheme.SONG
                and section is not prev
                and section.palette
            ):
                palette = Palette(list(section.palette))
                self._engine.set_palette(palette)
                self._album_hex = self._palette_to_hex(palette)
                self._maybe_publish()
        self._map_section = section
        return grid

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

    def _source_label(self) -> str:
        """Which audio path is currently driving the show (for diagnostics).

        ``live-tap`` = real audio decoded from the player; ``snapcast`` =
        snapserver tap; ``track-map`` = precomputed offline analysis replayed;
        ``metadata`` = the generic fallback animation (no real audio);
        ``idle`` = no source open yet.
        """
        src = self._source
        if src is None:
            return "idle"
        if isinstance(src, SnapcastSource):
            return "snapcast"
        if isinstance(src, MusicAssistantSource):
            return "live-tap"
        if isinstance(src, TrackMapSource):
            return "track-map"
        if isinstance(src, MetadataSource):
            return "metadata"
        return "unknown"

    def _compute_public_state(self) -> dict:
        """Now-playing / album-colour / tempo data exposed on the switch entity.

        Lets a dashboard card recolour itself to the extracted album palette and
        lock a visualizer to the song (position from the player, tempo here).
        """
        state: dict = {"audio_source": self._source_label()}
        if self._album_hex:
            state["album_colors"] = self._album_hex
        bg = self._last_beatgrid
        if bg is not None and bg.locked and bg.bpm > 0:
            state["bpm"] = round(bg.bpm)
        if self._map_section is not None:
            # Current track-map section loudness (0..1) for dashboards.
            state["section_energy"] = round(self._map_section.energy, 2)
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
        pos = attrs.get("media_position")
        if pos is None:
            return None
        live = float(pos)
        updated = attrs.get("media_position_updated_at")
        if updated is not None:
            try:
                live += max(0.0, (dt_util.utcnow() - updated).total_seconds())
            except (TypeError, ValueError):
                pass
        # The track map knows the beats at the *audible* position directly; the
        # live grid's phase refers to the analysis playhead (which leads the
        # speakers on a snapcast tap), so the map is both simpler and righter.
        tm = self._mapper.get(self._source.track_id) if self._source else None
        if tm is not None:
            mg = tm.grid_at(live)
            if mg is not None:
                bg = mg
        if bg is None or not bg.locked or bg.bpm <= 0:
            return None
        # Fold with the *rounded* bpm we publish, so the card (which reads that
        # integer bpm) and this anchor agree and the bars don't slowly drift.
        period = 60.0 / max(1.0, round(bg.bpm))
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
        await self._cancel_meta_upgrade()
        await self._mapper.close()
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
        # OpenSubsonic/Navidrome library (optional): (url, user, password) or None,
        # used to fetch & analyse library tracks MA won't expose a stream for.
        sub_url = (entry.options.get(CONF_SUBSONIC_URL, "") or "").strip()
        self._subsonic = (
            (
                sub_url,
                (entry.options.get(CONF_SUBSONIC_USER, "") or "").strip(),
                entry.options.get(CONF_SUBSONIC_PASSWORD, "") or "",
            )
            if sub_url
            else None
        )
        self._sessions: dict[str, SyncSession] = {}
        self._settings: dict[str, AreaSettings] = self._load_settings()
        # Live-feed subscribers per area (dashboard cards over the WS API).
        self._ws_subs: dict[str, set[Callable[[dict], None]]] = {}

    # -- live WebSocket feed -------------------------------------------------

    def ws_subscribe(self, area_id: str, cb: Callable[[dict], None]) -> Callable[[], None]:
        """Register a live-feed subscriber; returns an unsubscribe callable."""
        self._ws_subs.setdefault(area_id, set()).add(cb)

        def _unsub() -> None:
            subs = self._ws_subs.get(area_id)
            if subs is not None:
                subs.discard(cb)
                if not subs:
                    self._ws_subs.pop(area_id, None)

        return _unsub

    def ws_has_subs(self, area_id: str) -> bool:
        return bool(self._ws_subs.get(area_id))

    def ws_broadcast(self, area_id: str, payload: dict) -> None:
        for cb in tuple(self._ws_subs.get(area_id, ())):
            try:
                cb(payload)
            except Exception:  # noqa: BLE001 - one dead socket can't stop the rest
                pass

    def ws_snapshot(self, area_id: str) -> dict | None:
        """The meta payload for a subscriber joining mid-session."""
        session = self._sessions.get(area_id)
        return session.ws_meta() if session is not None else None

    def tap(self, area_id: str, group: str, strength: float = 0.95) -> None:
        """Route a drum-pad tap from the card to the area's live session."""
        session = self._sessions.get(area_id)
        if session is not None:
            session.manual_tap(group, strength)

    def set_drum_mode(self, area_id: str, active: bool) -> None:
        """Arm/release drum-pad mode for the area's live session."""
        session = self._sessions.get(area_id)
        if session is not None:
            session.set_drum_mode(active)

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
            subsonic=self._subsonic,
            on_finished=lambda: self._on_session_finished(area_id),
            ws_broadcast=lambda payload: self.ws_broadcast(area_id, payload),
            ws_active=lambda: self.ws_has_subs(area_id),
        )
        # Register before the (multi-second) handshake: the session's first
        # publish fires the area-update dispatcher, and a switch reconciling
        # against an unregistered session would briefly write off/on.
        self._sessions[area_id] = session
        try:
            await session.start()
        except BaseException:
            self._sessions.pop(area_id, None)
            raise

    def _on_session_finished(self, area_id: str) -> None:
        """A session ended on its own (e.g. lost DTLS); drop it and refresh."""
        if self._sessions.pop(area_id, None) is not None:
            name = getattr(self.configs.get(area_id), "name", area_id)
            _LOGGER.warning(
                "Music sync for %s ended on its own (DTLS lost beyond the "
                "reconnect window, or a sync-loop crash — see earlier log "
                "entries); the switch has been turned off", name,
            )
        async_dispatcher_send(self.hass, signal_area_update(area_id))

    async def stop_area(self, area_id: str) -> None:
        session = self._sessions.pop(area_id, None)
        if session is not None:
            await session.stop()
        async_dispatcher_send(self.hass, signal_area_update(area_id))

    async def async_shutdown(self) -> None:
        for area_id in list(self._sessions):
            await self.stop_area(area_id)
