"""Acquire Music Assistant audio and feed it to the analyzer in real time.

Strategy (per the approved plan): resolve a decodable URL for what the followed
player is currently playing, decode it with HA's bundled ffmpeg to mono PCM, and
*position-lock* the read to the player's reported playback position so pauses,
seeks and track changes stay aligned. Reading is paced to wall-clock so feature
frames line up with what the listener hears (offset by ``latency_ms``).

The Music-Assistant-specific bits (finding the player and a tappable URL) live in
:meth:`MusicAssistantSource._resolve` and are intentionally isolated and
defensive — the exact client attributes are confirmed by ``scripts/spike_ma.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import numpy as np

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE

_LOGGER = logging.getLogger(__name__)

# Resync if our decoded position drifts from the player by more than this.
_DRIFT_RESYNC_S = 0.5
_RESYNC_POLL_S = 1.0
_HOP_BYTES = ANALYSIS_HOP * 2  # s16le mono


@dataclass(slots=True)
class TrackInfo:
    """What the followed player is currently playing."""

    stream_url: str
    position: float  # seconds into the track
    track_id: str  # stable id used to detect track changes
    album_art_url: str | None
    is_live: bool  # True for non-seekable live streams (radio)


class PcmDecoder:
    """Wraps an ffmpeg subprocess decoding a URL to mono s16le PCM."""

    def __init__(self, ffmpeg_bin: str, sample_rate: int = ANALYSIS_SAMPLE_RATE) -> None:
        self._ffmpeg = ffmpeg_bin
        self._sr = sample_rate
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self, url: str, offset: float = 0.0) -> None:
        await self.stop()
        args = [self._ffmpeg, "-nostdin", "-loglevel", "error"]
        if offset > 0.05:
            args += ["-ss", f"{offset:.3f}"]
        args += [
            "-i", url,
            "-vn", "-ac", "1", "-ar", str(self._sr),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def read_hop(self) -> np.ndarray | None:
        """Read one hop of float32 samples (-1..1), or None at end of stream."""
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            raw = await self._proc.stdout.readexactly(_HOP_BYTES)
        except asyncio.IncompleteReadError as err:
            raw = err.partial
            if len(raw) < _HOP_BYTES:
                return None
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return samples

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass


class MusicAssistantSource:
    """Position-locked audio tap for a Music Assistant-backed media player."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        ffmpeg_bin: str,
        latency_ms: int = 0,
    ) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._latency = latency_ms / 1000.0
        self._decoder = PcmDecoder(ffmpeg_bin)
        self._frame_period = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE

        self._track_id: str | None = None
        self._album_art_url: str | None = None
        self._is_live = False
        self._decode_start_pos = 0.0  # track position at decoder start
        self._frames_emitted = 0
        self._wall0 = 0.0
        self._last_resync_check = 0.0
        self._mass_cache = None
        self._mass_cache_ts = 0.0

    @property
    def album_art_url(self) -> str | None:
        return self._album_art_url

    @property
    def track_id(self) -> str | None:
        return self._track_id

    def _mass_player_id(self) -> str | None:
        """The MA player id is the HA entity's unique id."""
        entry = er.async_get(self._hass).async_get(self._entity_id)
        return entry.unique_id if entry else None

    def _mass_client(self):
        """Best-effort lookup of the Music Assistant client (cached ~30s)."""
        now = time.monotonic()
        if self._mass_cache is not None and now - self._mass_cache_ts < 30.0:
            return self._mass_cache
        client = None
        for entry in self._hass.config_entries.async_entries("music_assistant"):
            runtime = getattr(entry, "runtime_data", None)
            mass = getattr(runtime, "mass", None) or runtime
            if mass is not None and hasattr(mass, "players"):
                client = mass
                break
        if client is None:
            data = self._hass.data.get("music_assistant")
            if data and hasattr(data, "players"):
                client = data
        self._mass_cache = client
        self._mass_cache_ts = now
        return client

    def _ha_position(self) -> tuple[float, bool]:
        """Return (estimated position seconds, is_playing) from HA state."""
        state = self._hass.states.get(self._entity_id)
        if state is None or state.state not in ("playing", "paused"):
            return 0.0, False
        pos = state.attributes.get("media_position")
        if pos is None:
            return 0.0, state.state == "playing"
        updated = state.attributes.get("media_position_updated_at")
        extra = 0.0
        if state.state == "playing" and updated is not None:
            extra = (dt_util.utcnow() - updated).total_seconds()
        return float(pos) + max(0.0, extra), state.state == "playing"

    def _resolve(self) -> TrackInfo | None:
        """Resolve the current track's tappable stream URL + position."""
        position, playing = self._ha_position()
        if not playing:
            return None

        state = self._hass.states.get(self._entity_id)
        attrs = state.attributes if state else {}
        is_live = attrs.get("media_duration") in (None, 0)

        stream_url: str | None = None
        album_art: str | None = None
        track_id: str | None = attrs.get("media_content_id")

        mass = self._mass_client()
        player_id = self._mass_player_id()
        if mass is not None and player_id is not None:
            try:
                player = mass.players.get(player_id)
                media = getattr(player, "current_media", None)
                if media is not None:
                    uri = getattr(media, "uri", None)
                    if uri and uri.startswith(("http://", "https://")):
                        stream_url = uri
                    album_art = getattr(media, "image_url", None)
                    track_id = getattr(media, "uri", None) or track_id
            except Exception as err:  # noqa: BLE001 - defensive across MA versions
                _LOGGER.debug("MA client lookup failed, falling back: %s", err)

        # Fallbacks via HA state.
        if album_art is None and attrs.get("entity_picture"):
            album_art = self._absolute_url(attrs["entity_picture"])
        if stream_url is None:
            # No direct tappable URL exposed; rely on HA-proxied content if usable.
            content = attrs.get("media_content_id")
            if content and content.startswith(("http://", "https://")):
                stream_url = content

        if not stream_url:
            _LOGGER.warning(
                "Could not resolve a decodable stream URL for %s; "
                "music sync needs Music Assistant to expose an HTTP stream",
                self._entity_id,
            )
            return None

        return TrackInfo(
            stream_url=stream_url,
            position=position,
            track_id=track_id or stream_url,
            album_art_url=album_art,
            is_live=bool(is_live),
        )

    def _absolute_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        try:
            base = self._hass.config.internal_url or self._hass.config.external_url or ""
        except Exception:  # noqa: BLE001
            base = ""
        return f"{base.rstrip('/')}{path}" if base else path

    async def open(self) -> bool:
        """Resolve and start decoding the current track. Returns success."""
        info = self._resolve()
        if info is None:
            return False
        await self._begin(info)
        return True

    async def _begin(self, info: TrackInfo) -> None:
        self._track_id = info.track_id
        self._album_art_url = info.album_art_url
        self._is_live = info.is_live
        start = 0.0 if info.is_live else max(0.0, info.position + self._latency)
        self._decode_start_pos = start
        self._frames_emitted = 0
        self._wall0 = time.monotonic()
        self._last_resync_check = self._wall0
        await self._decoder.start(info.stream_url, start)
        _LOGGER.debug("Decoding %s from %.2fs (live=%s)", self._entity_id, start, info.is_live)

    async def read_hop(self) -> np.ndarray | None:
        """Return the next paced hop, resyncing on drift/track-change.

        Returns None when playback has stopped (caller should idle).
        """
        now = time.monotonic()
        if now - self._last_resync_check >= _RESYNC_POLL_S:
            self._last_resync_check = now
            if not await self._maybe_resync():
                return None

        hop = await self._decoder.read_hop()
        if hop is None:
            # Stream ended (track boundary); try to pick up the next track.
            if await self._maybe_resync(force=True):
                hop = await self._decoder.read_hop()
            if hop is None:
                return None

        # Pace to wall clock so we don't outrun real playback.
        self._frames_emitted += 1
        target = self._wall0 + self._frames_emitted * self._frame_period
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        return hop

    async def _maybe_resync(self, force: bool = False) -> bool:
        """Restart the decoder if the track changed or position drifted.

        Returns False if nothing is playing.
        """
        info = self._resolve()
        if info is None:
            return False
        decoded_pos = self._decode_start_pos + self._frames_emitted * self._frame_period
        track_changed = info.track_id != self._track_id
        drift = abs(info.position + self._latency - decoded_pos)
        if force or track_changed or (not self._is_live and drift > _DRIFT_RESYNC_S):
            _LOGGER.debug(
                "Resync %s (changed=%s drift=%.2fs)", self._entity_id, track_changed, drift
            )
            await self._begin(info)
        return True

    async def close(self) -> None:
        await self._decoder.stop()
