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
from dataclasses import dataclass, field, replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .audio.map_source import TrackMapSource
from .audio.metadata import MetadataSource
from .audio.ma_stream import is_snapcast_backed
from .audio.snapcast import SnapcastSource
from .audio.source import (
    MusicAssistantSource,
    ha_track_query,
    ma_player_provider,
    resolve_map_url,
    resolve_next_map,
    track_label,
)
from .audio.structure import StructureTracker
from .audio.tempo import BeatGrid, TempoTracker
from .audio.track_index import TrackIndex
from .audio.trackmap import Section, TrackMapper
from .color.album_art import extract_palette, extract_weighted_palette
from .color.palette import Palette
from .color.song_palette import palette_from_chroma, should_update_palette
from .const import (
    ANALYSIS_HOP,
    ANALYSIS_SAMPLE_RATE,
    BANDS,
    CONF_ADVANCED,
    CONF_AREAS,
    CONF_AUTO_LEVELS,
    CONF_AUTO_TIMING,
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
    CONF_TUNABLES,
    DEFAULT_AUTO_LEVELS,
    DEFAULT_AUTO_TIMING,
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
    sanitize_tunables,
    signal_area_update,
)
from .effects.engine import EffectEngine
from .effects.modes import (
    UNRESTRAINED_MODES,
    AutoIntensityPicker,
    sanitize_auto_levels,
)
from .timing import TimingCalibrator
from .effects.safety import RELAXED_MAX_FLASHES_PER_S, FieldSafety
from .hue.bridge import EntertainmentConfig, HueBridge
from .hue.stream import DtlsStream, HueStreamEncoder
from .util import redact_url

_LOGGER = logging.getLogger(__name__)

_IDLE_FPS = 10
_IDLE_REOPEN_S = 2.0
# Ambient idle "show" (slow wandering glow) for a genuinely paused/empty room.
# It only emerges after the room has been idle this long, then fades its movement
# in over the following window — so a brief gap while the player moves from one
# song to the next (a couple of seconds at most) never triggers it and can't
# distract. Until then the calm palette glow holds, exactly as before.
_IDLE_SHOW_GRACE_S = 3.5
_IDLE_SHOW_FADE_S = 3.0

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

# Scheduled pre-drop window: how far ahead of a clearly-louder section boundary
# the engine is told a drop is coming (structure.drop_imminent + drop_eta_s).
# ~1 bar at 120 BPM — long enough for the pre-drop pull-down to read as
# deliberate tension, short enough that it always resolves.
_PREDROP_WINDOW_S = 2.0

# Album-art refresh: how long to wait for a new track's entity_picture to
# catch up with its title (HA writes them in separate state updates) before
# accepting an unchanged URL as "this track genuinely reuses the artwork",
# and how long to back off before retrying a failed extraction.
_ART_URL_GRACE_S = 6.0
_ART_RETRY_S = 10.0

# The colour schemes driven by album-art extraction (v1: hue-diverse gradient;
# v2: the cover's colours weighted by their real share of the artwork).
_ALBUM_SCHEMES = (ColorScheme.ALBUM_ART, ColorScheme.ALBUM_ART_V2)

# Live Song colours (tracks with no offline map): per-frame chroma EMA with a
# ~7 s time-constant at 50 fps, and a warm-up before the first palette so a
# few noisy seconds can't pick the key.
_CHROMA_ALPHA = 0.003
_CHROMA_WARMUP_FRAMES = 150  # ~3 s


def trackmap_cache_dir(hass) -> str:
    """The shared on-disk track-map cache directory (coordinator + pre-warm)."""
    return hass.config.path("hue_music_sync", "trackmaps")


def trackmap_cache_stats(hass) -> tuple[int, int]:
    """(number of cached track maps, total bytes on disk). Blocking — executor."""
    import os

    count = 0
    size = 0
    try:
        with os.scandir(trackmap_cache_dir(hass)) as it:
            for e in it:
                if e.name.endswith(".npz") and e.is_file():
                    count += 1
                    try:
                        size += e.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return count, size


DATA_TRACK_INDEX = "track_index"


def get_track_index(hass) -> TrackIndex:
    """The one shared per-track analysis-outcome index for this HA instance.

    Creation is IO-free; the file loads lazily (executor-side) on first async
    use, so this is safe to call from sync constructors. Sharing one instance
    between every area's mapper and the library pre-warm is the point: a track
    the sweep proved undecodable is not re-decoded at playback, failures
    survive restarts, and both sides agree on the disk-cache budget.
    """
    data = hass.data.setdefault(DOMAIN, {})
    idx = data.get(DATA_TRACK_INDEX)
    if idx is None:
        from pathlib import Path

        idx = TrackIndex(Path(trackmap_cache_dir(hass)) / "track_index.json")
        data[DATA_TRACK_INDEX] = idx
    return idx


def is_ma_player(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether a media_player entity is provided by the Music Assistant integration."""
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == "music_assistant"


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
    # The rungs Auto may pick from (ascending, non-empty). Only meaningful when
    # ``mode`` is Auto; ignored otherwise.
    auto_levels: tuple[SyncMode, ...] = DEFAULT_AUTO_LEVELS
    # Auto timing: calibrate the per-song startup delay instead of manual trim.
    auto_timing: bool = DEFAULT_AUTO_TIMING
    # Advanced live tunables (opt-in): show + apply the knob overrides. When off,
    # the engine uses the mode's coded params regardless of stored tunable values.
    advanced: bool = False
    tunables: dict[str, float] = field(default_factory=dict)

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
            auto_levels=sanitize_auto_levels(
                data.get(CONF_AUTO_LEVELS, DEFAULT_AUTO_LEVELS)
            ),
            auto_timing=bool(data.get(CONF_AUTO_TIMING, DEFAULT_AUTO_TIMING)),
            advanced=bool(data.get(CONF_ADVANCED, False)),
            tunables=sanitize_tunables(data.get(CONF_TUNABLES, {})),
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
            CONF_AUTO_LEVELS: [str(m) for m in self.auto_levels],
            CONF_AUTO_TIMING: self.auto_timing,
            CONF_ADVANCED: self.advanced,
            CONF_TUNABLES: dict(self.tunables),
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
        # Final safety stage (whole-field flash limiter + red guard). Every
        # emitted frame passes through the strict WCAG limiter normally, or the
        # relaxed high-budget limiter in Intense — transparent on real music, a
        # hard cap on pathological strobe output. EXTREME bypasses the limiter
        # entirely (the explicit "no limits" club mode, see _bypass_limiter and
        # the README photosensitivity warning), so its flashing is as sharp and
        # fast as the pipeline allows.
        self._safety = FieldSafety()
        self._safety_relaxed = FieldSafety(
            max_flashes_per_s=RELAXED_MAX_FLASHES_PER_S, calm_gated=False
        )
        self._was_unrestrained = False
        self._was_bypass = False
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
            # Shared with the library pre-warm: persistent failure verdicts +
            # a library-sized disk budget (no more eviction churn at 512).
            track_index=get_track_index(hass),
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
        self._meta_warned_track: str | None = None  # last track warned about
        self._source: (
            MusicAssistantSource | MetadataSource | SnapcastSource | TrackMapSource | None
        ) = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._stopping = False
        self._last_track: str | None = None
        self._last_art_url: str | None = None
        self._art_task: asyncio.Task | None = None
        self._reset_task: asyncio.Task | None = None  # player-switch source reset
        self._art_grace: float | None = None  # deadline for a lagging artwork URL
        self._art_retry_at = 0.0  # pacing after a failed extraction
        self._delay_buf: deque[tuple[float, dict]] = deque()
        # Now-playing / album-colour / tempo snapshot surfaced on the switch entity
        # so dashboard cards can recolour and lock a visualizer to the song.
        self.public_state: dict = {}
        self._album_hex: list[str] = []
        self._last_beatgrid: BeatGrid | None = None
        # Concrete level the Auto intensity currently resolves to. Persists across
        # track changes (a transient tempo unlock keeps the last level rather than
        # dipping) and defaults to Medium before any tempo has locked.
        self._auto_level: SyncMode = SyncMode.MEDIUM
        # Musical Auto-intensity picker: turns the live feature stream into a
        # rung, gated to the user's enabled set. Reset on every track change.
        self._auto_picker = AutoIntensityPicker()
        # Per-song light-timing calibrator (opt-in via settings.auto_timing):
        # estimates the startup slippage and locks a delay correction per track.
        self._timing_cal = TimingCalibrator()
        self._beat_anchor: float | None = None  # stable downbeat ref for the card
        self._last_publish = 0.0
        # Background probe that upgrades the metadata fallback to a real tap.
        self._upgrade_task: asyncio.Task | None = None
        self._upgrade_at = 0.0
        self._upgrade_interval = _META_UPGRADE_START_S
        self._prefetch_key: str | None = None  # next-track map already requested
        self._drum_until = 0.0  # drum-pad mode active while monotonic() < this
        # Live Song colours (no offline map): slow chroma/centroid EMA, what
        # the current palette was built from, and whether a map section
        # palette owns the colour this track (the map always wins).
        self._chroma_ema: list[float] | None = None
        self._chroma_frames = 0
        self._centroid_ema = 0.5
        self._chroma_applied: list[float] | None = None
        self._chroma_applied_at = 0.0
        self._song_palette_from_map = False

    @property
    def settings(self) -> AreaSettings:
        return self._settings

    def _effective_mode(self, mode: SyncMode) -> SyncMode:
        """Concrete engine mode for a setting: Auto resolves to its current level.

        The engine only ever handles concrete modes; Auto is a session concept
        resolved from the live tempo (see :meth:`_maybe_apply_auto_intensity`).
        """
        return self._auto_level if mode is SyncMode.AUTO else mode

    async def start(self) -> None:
        self._engine.set_tunables(
            self._settings.tunables if self._settings.advanced else {}
        )
        self._engine.set_mode(self._effective_mode(self._settings.mode))
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
        # The bridge silently ignores a new handshake while a previous DTLS
        # session still lingers (up to ~10 s after an unclean end — an HA
        # restart, a crash, a kill). Retry across that window with a fresh
        # stream activation each time instead of failing the switch outright.
        # ``_stopping`` is checked at every step: the user can flip the switch
        # off while a (multi-second) handshake attempt is still in flight, and
        # without the checks the handshake completing AFTER stop() would
        # re-open the stream as an untracked ghost session nothing can stop.
        last_err: Exception | None = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(4.0)
            if self._stopping:
                raise HomeAssistantError("sync was turned off while starting")
            await self._bridge.start_stream(self._config.id)
            try:
                await self._stream.start()
                last_err = None
                break
            except Exception as err:  # noqa: BLE001
                last_err = err
                _LOGGER.debug(
                    "DTLS handshake attempt %d/3 for %s failed: %s",
                    attempt + 1, self._config.name, err,
                )
                try:
                    await self._bridge.stop_stream(self._config.id)
                except Exception:  # noqa: BLE001 - best-effort before retry
                    pass
        if last_err is not None:
            raise last_err
        if self._stopping:
            # stop() ran while the successful handshake was in flight and could
            # not see this connection yet: release it here instead of leaving a
            # live DTLS channel (and its keepalive) that survives the stop.
            await self._safe_release_stream()
            raise HomeAssistantError("sync was turned off while starting")
        self._running = True
        self._task = self._hass.async_create_background_task(
            self._run(), f"hue_music_sync_{self._config.id}"
        )

    def apply_settings(self, settings: AreaSettings) -> None:
        """Live-apply changed settings to the running session."""
        prev = self._settings
        self._settings = settings
        # Advanced tunables first, so the mode below is built with them folded in.
        self._engine.set_tunables(settings.tunables if settings.advanced else {})
        self._engine.set_mode(self._effective_mode(settings.mode))
        self._engine.set_effect(settings.effect)
        self._engine.set_brightness(settings.brightness)
        if settings.mode != prev.mode or settings.effect != prev.effect:
            # The flash limiter holds a slow brightness anchor and freezes it
            # while it's limiting, so a mode/effect change carries the *previous*
            # mode's field level forward: switching e.g. Subtle (steady ~0.8) to a
            # dark club mode would pin the room near the old bright anchor and
            # suppress the new mode's flashing until the session was restarted.
            # Clear them so the new mode anchors to its own field from this frame.
            self._safety.reset()
            self._safety_relaxed.reset()
            self._was_unrestrained = self._unrestrained()
            self._last_safe_t = None
        if settings.auto_levels != prev.auto_levels:
            # Let a checklist change re-clamp the pick on the next frame instead
            # of waiting out the dwell — toggling a rung feels immediate.
            self._auto_picker.allow_immediate_repick()
        if settings.auto_timing and not prev.auto_timing:
            # Newly enabled mid-song: measure from now instead of applying a
            # stale value from a previous track.
            self._timing_cal.reset()
        if settings.colour != prev.colour:
            self._apply_colour()
            # Re-extract album art if switching to Album (both guards, or the
            # "same artwork" check below would skip the extraction we want).
            self._last_track = None
            self._last_art_url = None
            self._art_grace = None
            self._art_retry_at = 0.0
            self._map_section = None  # re-apply Song section palette on next poll
            # Live Song colours restart cleanly too (the map reclaims first).
            self._chroma_applied = None
            self._chroma_applied_at = 0.0
            self._song_palette_from_map = False
        if settings.media_player != prev.media_player:
            # Player changed: drop the current source so the loop re-opens it on
            # the new one. Unconditional (a no-op without a source) because it
            # also cancels an in-flight upgrade probe — one opened against the
            # *old* player would otherwise be installed as the source moments
            # after the switch.
            # Tracked (not fire-and-forget) so stop() can cancel it and a rapid
            # second player switch supersedes an in-flight reset cleanly.
            if self._reset_task is not None and not self._reset_task.done():
                self._reset_task.cancel()
            self._reset_task = self._hass.async_create_task(self._reset_source())

    def _maybe_apply_auto_intensity(
        self, frame, beatgrid: BeatGrid | None, dt: float
    ) -> None:
        """When Auto is selected, resolve the music to a rung and apply it.

        Feeds the live features (loudness, salience, tempo, beat) to the musical
        picker, which spreads them ACROSS the user's enabled set
        (``settings.auto_levels``): calm passages sit on the lowest enabled rung,
        big moments reach the highest. The picker owns its own smoothing,
        hysteresis and dwell, so this only reacts on an actual change.
        """
        if self._settings.mode is not SyncMode.AUTO:
            return
        bpm = beatgrid.bpm if beatgrid is not None and beatgrid.locked else 0.0
        # The song's own intensity profile (offline map playback stamps it on the
        # frame; live tap / metadata leave the neutral defaults, so the picker
        # keeps its fixed window). It spreads the enabled rungs across THIS song's
        # quiet↔loud range and shades the pick by its mood.
        prof_kw = {}
        if frame.intensity_lo is not None and frame.intensity_hi is not None:
            prof_kw = dict(
                lo=frame.intensity_lo,
                hi=frame.intensity_hi,
                dynamics=frame.intensity_dynamics,
                mood=frame.intensity_mood,
            )
        target = self._auto_picker.update(
            dt,
            energy=frame.energy,
            salience=frame.salience,
            bpm=bpm,
            beat=frame.beat,
            allowed=self._settings.auto_levels,
            # Offline lag-free section-intensity (map playback): mapped directly so
            # the rung switch lands on time. None on live/metadata → picker smooths.
            signal=frame.intensity_signal,
            **prof_kw,
        )
        if target is self._auto_level:
            return
        self._auto_level = target
        # Just swap the mode's parameters — the engine's brightness/colour state
        # carries across so the room eases into the new rung. Crucially, DON'T
        # reset the flash limiters here: that clears their slow anchor and makes
        # the room visibly hang for a beat before it reacts. The one reset that
        # is actually needed — when the switch crosses the strict<->relaxed
        # limiter boundary (High<->Intense) — is handled in `_safe_send` off the
        # `_unrestrained()` transition, so adjacent Auto switches stay seamless.
        self._engine.set_mode(target)

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
        # Preset themes are static palettes. Album art (v1/v2) and Song are
        # dynamic (extracted per track / per section), and keep the engine
        # fallback palette until their colours are ready.
        if self._settings.colour not in (*_ALBUM_SCHEMES, ColorScheme.SONG):
            self._engine.set_scheme(self._settings.colour)

    def _reset_rhythm_models(self) -> None:
        """Clear the tempo/structure/track state for a clean re-lock.

        Used whenever the audio timeline jumps under us — a track change, a
        source reset, or swapping the metadata fallback for a real tap — so the
        previous track's locked grid can't keep firing stale beats on the new one.
        """
        self._last_track = None
        self._last_art_url = None
        self._art_grace = None
        self._play_track = None
        self._tempo.reset()
        self._structure.reset()
        self._beat_anchor = None  # downbeat ref is stale after a discontinuity
        self._map_track = None
        self._map_prev_pos = None
        self._map_section = None
        self._prefetch_key = None  # re-evaluate the next track after a jump
        # The Auto picker is reset at the track-change site in the run loop (so a
        # new song re-picks at once from its own section curve rather than
        # carrying the previous rung); a bare source reset here keeps the current
        # pick until the track id actually changes. The timing calibrator IS
        # per-song: the startup slippage is measured fresh each track.
        self._timing_cal.reset()

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
        playing = [
            s.entity_id
            for s in self._hass.states.async_all("media_player")
            if s.state == "playing"
        ]
        _LOGGER.debug("Playing media_players: %s", playing)
        for entity_id in playing:
            if is_ma_player(self._hass, entity_id):
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
        # The snapcast tap is only ever offered for a Music Assistant player:
        # ma_player_provider() returns None for anything else, and
        # is_snapcast_backed(None) is deliberately True, so without this a
        # followed non-MA player (a Subsonic radio, say) would fall into the
        # snapserver's "use the playing stream" fallback and sync the lights to
        # a different room's audio.
        if (
            self._snapserver_host
            and is_ma_player(self._hass, entity_id)
            and is_snapcast_backed(ma_player_provider(self._hass, entity_id))
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
            if meta.track_id and meta.track_id != self._meta_warned_track:
                # Once per track, at WARNING so it survives in the system log:
                # a track on the generic animation is the #1 "sync doesn't work
                # on this song" report, and the reason is otherwise invisible.
                self._meta_warned_track = meta.track_id
                url = resolve_map_url(self._hass, entity_id, self._subsonic)
                if url is None:
                    # We only get here after TrackMapSource.open() has *awaited*
                    # the library match and come back empty, so the song genuinely
                    # isn't matchable — say so, rather than blaming a Subsonic URL
                    # the user has very likely already set.
                    query = ha_track_query(self._hass, entity_id)
                    reason = (
                        "no per-track stream URL could be resolved from Music "
                        "Assistant, and the song could not be matched to a track "
                        "in the Music Assistant library by its title/artist "
                        "(%r by %r). If it *is* a library track, check that its "
                        "tags match, set the OpenSubsonic/Navidrome URL + login "
                        "in the integration options, or run the "
                        "hue_music_sync.prewarm_library service once"
                        % (query.title, query.artist)
                    )
                else:
                    reason = (
                        "offline analysis has not produced a servable track map. "
                        "Decodable tracks now always get at least the ambient "
                        "tier, so landing here means the audio could not be "
                        "fetched/decoded (or analysis is still pending) — "
                        "diagnose it with the hue_music_sync.analyze_track "
                        "service; see earlier log entries"
                    )
                _LOGGER.warning(
                    "%s: track %r is using the generic metadata animation — %s",
                    self._config.name, meta.track_id, reason,
                )
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
        idle_phase = 0.0
        idle_since: float | None = None  # when the room last went idle (else None)
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
                            if idle_since is None:
                                idle_since = now
                            idle_phase += dt
                            await self._send_idle(idle_phase, now - idle_since)
                            await asyncio.sleep(1.0 / _IDLE_FPS)
                            continue

                    # Paused/stopped: idle (keep the source for a fast resume).
                    pstate = self._hass.states.get(self._source.entity_id)
                    if pstate is None or pstate.state != "playing":
                        if idle_since is None:
                            idle_since = now
                        idle_phase += dt
                        await self._send_idle(idle_phase, now - idle_since)
                        await asyncio.sleep(1.0 / _IDLE_FPS)
                        continue

                    frame = await self._source.read_frame()  # paced to real time
                    if frame is None:
                        await self._reset_source()
                        continue
                    # Real audio is flowing again: leave idle. (A brief between-song
                    # gap clears this as soon as the next frame arrives — before the
                    # grace elapses — so the ambient show never triggers on it.)
                    idle_since = None

                    self._maybe_refresh_album_art()
                    if self._settings.colour == ColorScheme.SONG and frame.chroma:
                        self._update_live_chroma(frame)
                    if now - self._map_check >= 1.0:
                        self._map_check = now
                        self._maybe_track_map()
                        self._maybe_prefetch_next()
                        self._maybe_live_song_palette(now)
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
                        # Re-evaluate Auto for the NEW song at once: clear the
                        # picker's carried level/envelope so the previous track's
                        # rung can't linger for a couple of seconds, and drop the
                        # dwell so the new song's opening rung applies immediately
                        # (its own section curve makes that opening honest).
                        self._auto_picker.reset()
                        self._auto_picker.allow_immediate_repick()
                        # Fresh live-chroma state: the new song picks its own
                        # key, and the map (if it arrives) reclaims the colour.
                        self._chroma_ema = None
                        self._chroma_frames = 0
                        self._chroma_applied = None
                        self._chroma_applied_at = 0.0
                        self._song_palette_from_map = False
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
                    self._maybe_apply_auto_intensity(frame, beatgrid, period)
                    if self._settings.auto_timing and not self._timing_cal.locked:
                        self._feed_timing_calibrator(beatgrid, period)
                    # Drum-pad mode (card taps drive the beats) auto-expires, so
                    # set it from the live window every frame before rendering.
                    self._engine.set_manual_only(now < self._drum_until)
                    # Honest wall-clock dt (clamped to 0.5x-2x the nominal frame
                    # period): the engine's decays, waves and predrop timing stay
                    # true under scheduler jitter instead of assuming a perfect
                    # frame every time. A steady loop is unchanged.
                    render_dt = min(2.0 * period, max(0.5 * period, dt))
                    colors = self._engine.render(frame, render_dt, beatgrid, structure)
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

    async def _send_idle(self, phase: float, idle_elapsed: float) -> None:
        # Calm palette glow that, once the room has been idle past the grace
        # window, blossoms into the slow wandering "show" — its movement fading in
        # over ``_IDLE_SHOW_FADE_S``. A brief between-song gap stays below the
        # grace, so it never sees more than the calm glow.
        show = max(
            0.0, min(1.0, (idle_elapsed - _IDLE_SHOW_GRACE_S) / _IDLE_SHOW_FADE_S)
        )
        await self._safe_send(self._engine.render_idle_show(phase, show))

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
        # Auto timing (opt-in): the calibrator's per-song correction replaces the
        # manual trim on top of the same baseline; falls back to the manual
        # offset until it has a value (and on sources with no position data).
        trim_ms = self._settings.timing_ms
        if self._settings.auto_timing:
            auto = self._timing_cal.offset_ms
            if auto is not None:
                trim_ms = auto
        delay_s = max(0.0, (base_ms + trim_ms) / 1000.0)
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
        """True in the explicit club modes that run the relaxed flash limiter.

        Uses the *effective* mode so an Auto pick of Intense/Extreme runs the
        same relaxed limiter as picking it by hand — Auto's Intense is Intense.
        """
        return (
            self._effective_mode(self._settings.mode) in UNRESTRAINED_MODES
            and self._settings.effect is not SyncEffect.MOVIES
        )

    def _bypass_limiter(self) -> bool:
        """True in Extreme (non-Movies): the flash limiter is skipped entirely.

        Extreme is the explicit "no limits" club mode (README photosensitivity
        warning) tuned for heavy, fast tracks — the user wants the sharpest,
        fastest flashing the lights can do, so the final whole-field limiter
        does not touch the frame. Every other mode (Intense included) keeps its
        limiter.
        """
        return (
            self._effective_mode(self._settings.mode) is SyncMode.EXTREME
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
        bypass = self._bypass_limiter()
        unrestrained = self._unrestrained()
        if bypass:
            # Extreme: no limiter at all. Keep the limiters' field history clean
            # so they engage correctly if the user switches back to another mode.
            if not self._was_bypass:
                self._safety.reset()
                self._safety_relaxed.reset()
        else:
            limiter = self._safety_relaxed if unrestrained else self._safety
            if unrestrained != self._was_unrestrained or self._was_bypass:
                limiter.reset()  # field history is stale after a limiter/bypass switch
            colors = limiter.process(colors, dt)
        self._was_unrestrained = unrestrained
        self._was_bypass = bypass
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
            # Event-selection evidence (calibration instruments for the
            # salience/width gates): absolute track-relative loudness and
            # onset broadbandness of this frame.
            "salience": round(frame.salience, 2),
            "width": round(frame.onset_width, 2),
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
            if not self._running or self._stopping:
                # stop() ran while this handshake was in flight and couldn't
                # see the new connection: release it instead of leaving a ghost
                # DTLS session (with a live keepalive) the switch can't stop.
                await self._safe_release_stream()
                return False
            self._delay_buf.clear()  # drop stale frames buffered before the drop
            self._safety.reset()  # field history is stale after the gap
            self._safety_relaxed.reset()
            self._last_safe_t = None
            _LOGGER.info("Reconnected DTLS stream for %s", self._config.name)
            return True
        return False

    def _maybe_track_map(self) -> None:
        """Kick off offline analysis of the current track (once per track)."""
        src = self._source
        if src is None:
            return
        if isinstance(src, TrackMapSource):
            # It owns its own map lifecycle (including the library match that
            # re-keys the track), and resolve_map_url() here would be resolving
            # a *different* key than the one it is playing under.
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
        st = self._hass.states.get(src.entity_id)
        label = track_label(
            st.attributes.get("media_artist") if st else None,
            st.attributes.get("media_title") if st else None,
        )
        self._mapper.ensure(track, url, label)

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

    def _audible_position(self) -> float | None:
        """The player's reported audible position (s): media_position + elapsed.

        Distinct from :meth:`_analysis_position` — this is where the *speakers*
        are, never the analysis playhead — so the calibrator can compare the two.
        """
        src = self._source
        if src is None:
            return None
        state = self._hass.states.get(src.entity_id)
        if state is None or state.state != "playing":
            return None
        pos = state.attributes.get("media_position")
        if pos is None:
            return None
        live = float(pos)
        updated = state.attributes.get("media_position_updated_at")
        if updated is not None:
            try:
                live += max(0.0, (dt_util.utcnow() - updated).total_seconds())
            except (TypeError, ValueError):
                pass
        return live

    def _feed_timing_calibrator(self, beatgrid: BeatGrid | None, dt: float) -> None:
        """Sample the analyser-vs-audible gap into the per-song timing calibrator.

        The analyser playhead is the live tap's decoded position (or a map
        source's own analysis position); the audible reference is the player's
        media position. ``expected_lead`` is the seek-ahead the source already
        intends, so the no-hang deviation is ~0 and the working baseline stays.
        """
        src = self._source
        if src is None:
            return
        analyzer_pos = getattr(src, "decoded_position", None)
        if analyzer_pos is None:
            analyzer_pos = getattr(src, "analysis_position", None)
        lead_ms = getattr(src, "playback_lead_ms", 0) or 0
        expected_lead_s = (
            lead_ms if lead_ms > 0 else self._settings.latency_ms
        ) / 1000.0
        beat_period_s = (
            60.0 / beatgrid.bpm
            if beatgrid is not None and beatgrid.locked and beatgrid.bpm > 0
            else None
        )
        self._timing_cal.update(
            dt,
            analyzer_pos=analyzer_pos,
            audible_pos=self._audible_position(),
            expected_lead_s=expected_lead_s,
            playing=True,
            beat_period_s=beat_period_s,
        )

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
                and boundary[0] <= _PREDROP_WINDOW_S
                and boundary[1] > section.energy + 0.15
            ):
                structure.drop_imminent = True
                structure.drop_eta_s = boundary[0]
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
                # The map's per-section harmony owns the Song colour for the
                # rest of this track; the live-chroma path stands down.
                self._song_palette_from_map = True
                self._maybe_publish()
        self._map_section = section
        return grid

    def _update_live_chroma(self, frame) -> None:
        """Fold this frame's chroma/centroid into the slow live EMA (~7 s)."""
        c = frame.chroma
        if self._chroma_ema is None or len(self._chroma_ema) != len(c):
            self._chroma_ema = list(c)
        else:
            ema = self._chroma_ema
            for i, v in enumerate(c):
                ema[i] += (v - ema[i]) * _CHROMA_ALPHA
        self._centroid_ema += (frame.centroid - self._centroid_ema) * _CHROMA_ALPHA
        self._chroma_frames += 1

    def _maybe_live_song_palette(self, now: float) -> None:
        """Song colours from the LIVE chroma when no track map supplies them.

        The offline map's per-section palettes always take precedence — this
        only fills in for tracks that never got one (radio, untappable or
        unanalysed songs), so harmony-driven colour works everywhere. Applies
        are throttled (see should_update_palette) so the room's colour moves
        on genuine harmonic shifts, never churns.
        """
        if (
            self._settings.colour != ColorScheme.SONG
            or self._song_palette_from_map
            or self._chroma_ema is None
            or self._chroma_frames < _CHROMA_WARMUP_FRAMES
        ):
            return
        if not should_update_palette(
            self._chroma_applied, self._chroma_ema, now - self._chroma_applied_at
        ):
            return
        palette = palette_from_chroma(self._chroma_ema, self._centroid_ema)
        if palette is None:
            return
        self._engine.set_palette(palette)
        self._album_hex = self._palette_to_hex(palette)
        self._chroma_applied = list(self._chroma_ema)
        self._chroma_applied_at = now
        self._maybe_publish()

    def _maybe_refresh_album_art(self) -> None:
        # Only when an Album colour (v1 or v2) is selected; presets are static.
        if self._settings.colour not in _ALBUM_SCHEMES or self._source is None:
            return
        now = time.monotonic()
        if now < self._art_retry_at:
            return  # backing off after a failed extraction
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
        if url == self._last_art_url:
            # A new track but the artwork URL hasn't changed. Usually HA just
            # hasn't written the new entity_picture yet (title lands first,
            # artwork a moment later) — claiming the track NOW would freeze the
            # colours on the previous song's palette. Give the artwork a grace
            # window to catch up; only if the URL genuinely stays the same
            # (same-album tracks, a library match re-keying the song mid-track)
            # accept it and keep the current palette.
            if self._art_grace is None:
                self._art_grace = now + _ART_URL_GRACE_S
            if now < self._art_grace:
                return  # not claimed yet: keep polling for the new artwork
            self._art_grace = None
            self._last_track = track
            return
        # Only now claim the track as handled, since we're actually extracting it.
        self._art_grace = None
        self._last_track = track
        self._last_art_url = url
        self._art_task = self._hass.async_create_task(self._extract_art(url))

    async def _extract_art(self, url: str) -> None:
        scheme = self._settings.colour
        if scheme == ColorScheme.ALBUM_ART_V2:
            palette = await extract_weighted_palette(self._ffmpeg, url)
        else:
            palette = await extract_palette(self._ffmpeg, url)
        if palette is not None and self._settings.colour == scheme:
            self._engine.set_palette(palette)
            self._album_hex = self._palette_to_hex(palette)
            self._maybe_publish()  # surface the new album colours to cards at once
            _LOGGER.info(
                "Album colours for %s: %s (weights %s)",
                self._config.name,
                [tuple(round(v, 2) for v in c) for c in palette.colors],
                [round(w, 2) for w in palette.weights] if palette.weights else None,
            )
        elif palette is None:
            _LOGGER.warning(
                "Album-art extraction failed for %s (%s)",
                self._config.name, redact_url(url),
            )
            # Un-claim the track so it's retried (paced): a transient artwork
            # fetch failure must not strand the colours on the previous song.
            self._last_track = None
            self._last_art_url = None
            self._art_retry_at = time.monotonic() + _ART_RETRY_S

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
        # When Auto intensity is active, surface the level it resolved to so the
        # card can show e.g. "Auto · High".
        if self._settings.mode is SyncMode.AUTO:
            state["auto_mode"] = str(self._auto_level)
            # The rungs Auto may pick from, so the card can show/drive the
            # enabled-set checklist.
            state["auto_levels"] = [str(m) for m in self._settings.auto_levels]
        # Auto timing: surface the live calibrated correction + lock state so the
        # card's Timing control can show "Auto ⟳ / Auto +120 ms".
        if self._settings.auto_timing:
            state["auto_timing"] = True
            off = self._timing_cal.offset_ms
            if off is not None:
                state["timing_auto_ms"] = off
            state["timing_locked"] = self._timing_cal.locked
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
        """Tear the session down. Must never raise and never hang.

        The bridge-facing resources are released FIRST and unconditionally:
        if the DTLS channel isn't closed (its keepalive would keep the ghost
        session alive on the bridge indefinitely) or the entertainment area
        isn't handed back, every later handshake times out waiting for the
        bridge's HelloVerify — the "switch stuck off, turning on again fails"
        failure. The audio teardown (ffmpeg, snapcast, analysis tasks) happens
        after, each step bounded and best-effort.
        """
        self._stopping = True
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                # Let the render loop actually exit before yanking the socket
                # out from under a mid-flight send.
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except BaseException:  # noqa: BLE001 - cancelled/timeout/crash: proceed
                pass
        # In-flight background tasks (album-art extraction owns an ffmpeg
        # subprocess; a player-switch source reset touches the source) must
        # not outlive the session.
        for bg in (self._art_task, self._reset_task):
            if bg is not None and not bg.done():
                bg.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(bg), timeout=2.0)
                except BaseException:  # noqa: BLE001 - cancelled/timeout: proceed
                    pass
        self._art_task = None
        self._reset_task = None
        try:
            await self._stream.stop()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error closing DTLS stream for %s: %s", self._config.name, err)
        try:
            await self._bridge.stop_stream(self._config.id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error stopping bridge stream for %s: %s", self._config.name, err)
        for label, closer in (
            ("meta upgrade", self._cancel_meta_upgrade()),
            ("track mapper", self._mapper.close()),
            ("audio source", self._close_source()),
        ):
            try:
                await asyncio.wait_for(closer, timeout=5.0)
            except Exception as err:  # noqa: BLE001 - bounded, best-effort
                _LOGGER.debug(
                    "Teardown of %s for %s failed: %s", label, self._config.name, err
                )
        await self._restore_snapshot()

    async def _close_source(self) -> None:
        source = self._source
        self._source = None
        if source is not None:
            await source.close()

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

        Now-playing keys are present only while syncing; the persistent advanced
        tunables are always carried so the card can show its knob section (and
        their values) whether or not the area is currently playing.
        """
        session = self._sessions.get(area_id)
        attrs = dict(session.public_state) if session is not None else {}
        s = self.get_settings(area_id)
        attrs["advanced"] = s.advanced
        if s.advanced and s.tunables:
            attrs["tunables"] = dict(s.tunables)
        return attrs

    def player_candidates(self, area_id: str) -> dict:
        """The players this area may be told to follow (for the card's picker).

        The active Music Assistant players — the short, useful list — plus the
        pinned and currently-followed players even when they are neither active
        nor Music Assistant's: a Subsonic radio is not an MA player and must not
        be missing from its own picker. ``selected`` is None when the area is on
        the automatic pick (the card offers that as "Auto").
        """
        selected = self.get_settings(area_id).media_player
        following = self.area_attributes(area_id).get("source_player")
        pinned = {eid for eid in (selected, following) if eid}
        players: list[dict] = []
        for state in self.hass.states.async_all("media_player"):
            entity_id = state.entity_id
            mass = is_ma_player(self.hass, entity_id)
            active = state.state in ("playing", "paused")
            if not ((mass and active) or entity_id in pinned):
                continue
            pinned.discard(entity_id)
            attrs = state.attributes
            players.append(
                {
                    "entity_id": entity_id,
                    "name": attrs.get("friendly_name") or entity_id,
                    "state": state.state,
                    "title": attrs.get("media_title"),
                    "artist": attrs.get("media_artist"),
                    "music_assistant": mass,
                }
            )
        # A pinned player that no longer exists still gets an entry, so the card
        # can show *why* nothing is syncing and let the user pick something else.
        for entity_id in pinned:
            players.append(
                {
                    "entity_id": entity_id,
                    "name": entity_id,
                    "state": "unavailable",
                    "title": None,
                    "artist": None,
                    "music_assistant": False,
                }
            )
        players.sort(
            key=lambda p: (
                not p["music_assistant"],
                p["state"] != "playing",
                p["name"].lower(),
            )
        )
        return {"players": players, "selected": selected, "following": following}

    async def update_settings(self, area_id: str, **changes) -> None:
        current = self.get_settings(area_id)
        updated = replace(current, **changes)
        self._settings[area_id] = updated
        if (session := self._sessions.get(area_id)) is not None:
            session.apply_settings(updated)
        # Refresh the area's entities (e.g. the Advanced switch, and the sync
        # switch's attributes the card reads) so a change from any surface shows
        # everywhere.
        async_dispatcher_send(self.hass, signal_area_update(area_id))
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
                # Already under the shared start/stop lock — use the unlocked
                # variant so we don't deadlock re-acquiring it.
                await manager._stop_area_locked(other_id)

    def _start_stop_lock(self) -> asyncio.Lock:
        """One integration-wide lock serialising every start/stop.

        Rapid switch toggles (on/off/on pressed quickly) spawn concurrent
        service-call tasks. Without serialising them a ``stop`` could pop and stop
        a session while a racing ``start`` — having seen it gone — builds a new
        one, or a session could be popped mid-``start()``: the stream keeps
        running while ``is_active`` reports False, so the switch shows "off" while
        the room stays synced and can't be turned off. Only one area streams at a
        time anyway, so a single shared lock is the natural guard; the internal
        ``*_locked`` helpers never re-acquire it, so there is no deadlock even
        when ``_enforce_single_active_area`` stops areas on other managers.
        """
        data = self.hass.data.setdefault(DOMAIN, {})
        lock = data.get("_start_stop_lock")
        if lock is None:
            lock = asyncio.Lock()
            data["_start_stop_lock"] = lock
        return lock

    async def start_area(self, area_id: str) -> None:
        async with self._start_stop_lock():
            await self._start_area_locked(area_id)

    async def _start_area_locked(self, area_id: str) -> None:
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
        async with self._start_stop_lock():
            await self._stop_area_locked(area_id)

    async def _stop_area_locked(self, area_id: str) -> None:
        session = self._sessions.pop(area_id, None)
        if session is not None:
            await session.stop()
        async_dispatcher_send(self.hass, signal_area_update(area_id))

    async def async_shutdown(self) -> None:
        for area_id in list(self._sessions):
            await self.stop_area(area_id)
