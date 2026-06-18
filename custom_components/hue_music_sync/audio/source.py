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
from dataclasses import dataclass, field

import numpy as np

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE
from .analyzer import AnalysisFrame, Analyzer
from .ma_stream import attr_summary, iter_http_urls, ma_stream_variants
from .subsonic import is_subsonic_provider, subsonic_stream_url

# (url, username, password) for an OpenSubsonic/Navidrome library, or None.
SubsonicCfg = tuple[str, str, str] | None

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
    # Alternate stream URLs to try if the primary won't decode (covers
    # flow-vs-single and output-codec differences between MA player types).
    alt_urls: list[str] = field(default_factory=list)


def _find_mass_client(hass: HomeAssistant):
    """Best-effort lookup of the Music Assistant client object."""
    for entry in hass.config_entries.async_entries("music_assistant"):
        runtime = getattr(entry, "runtime_data", None)
        mass = getattr(runtime, "mass", None) or runtime
        if mass is not None and hasattr(mass, "players"):
            return mass
    data = hass.data.get("music_assistant")
    if data and hasattr(data, "players"):
        return data
    return None


def ma_player_provider(hass: HomeAssistant, entity_id: str) -> str | None:
    """The Music Assistant provider backing this player (e.g. ``snapcast``,
    ``sendspin``, ``slimproto``), or None when it cannot be determined."""
    try:
        mass = _find_mass_client(hass)
        entry = er.async_get(hass).async_get(entity_id)
        player_id = entry.unique_id if entry else None
        if mass is None or player_id is None:
            return None
        player = mass.players.get(player_id)
        provider = getattr(player, "provider", None)
        return str(provider) if provider else None
    except Exception as err:  # noqa: BLE001 - defensive across MA versions
        _LOGGER.debug("[%s] provider lookup failed: %s", entity_id, err)
        return None


def _subsonic_candidate(sd, subsonic: SubsonicCfg) -> str | None:
    """Build a Subsonic stream URL for the current item, if applicable.

    When the queue item's provider is (Open)Subsonic and the user configured a
    library URL + login, build the ``/rest/stream`` URL from the provider track
    id (``streamdetails.item_id``) — the reliable way to get library audio that
    Music Assistant won't expose a tappable URL for (e.g. Sendspin playback).
    """
    if not subsonic or sd is None:
        return None
    url, user, password = subsonic
    if not url:
        return None
    if is_subsonic_provider(getattr(sd, "provider", None)):
        item_id = getattr(sd, "item_id", None)
        built = subsonic_stream_url(url, user, password, item_id)
        if built is None:
            # A Subsonic track we *should* be able to stream, but a required
            # field is missing — surface it so a misconfigured library/login or
            # an item with no id doesn't just silently fall through to metadata.
            _LOGGER.warning(
                "Subsonic track has no streamable URL (item_id=%r, user set=%s, "
                "password set=%s); check the OpenSubsonic library URL/login option",
                item_id, bool(user), bool(password),
            )
        return built
    return None


def resolve_map_url(
    hass: HomeAssistant, entity_id: str, subsonic: SubsonicCfg = None
) -> str | None:
    """A per-track URL suitable for *offline* analysis of the current track.

    Prefers the queue item's provider stream path (a plain file/HTTP stream that
    decodes faster than realtime), then a configured OpenSubsonic library URL,
    then an http ``current_media.uri`` / any discoverable URL. Returns None when
    the player exposes nothing analysable per-track (radio, flow streams).
    """
    try:
        mass = _find_mass_client(hass)
        entry = er.async_get(hass).async_get(entity_id)
        player_id = entry.unique_id if entry else None
        if mass is None or player_id is None:
            return None
        player = mass.players.get(player_id)
        media = getattr(player, "current_media", None) if player else None
        item = sd = None
        if player is not None:
            src = getattr(player, "active_source", None)
            queue = mass.player_queues.get(src) if src else None
            item = getattr(queue, "current_item", None)
            sd = getattr(item, "streamdetails", None)
            path = getattr(sd, "path", None)
            if isinstance(path, str) and path.startswith(("http://", "https://")):
                return path
            # OpenSubsonic/Navidrome library track: build the stream URL ourselves.
            sub = _subsonic_candidate(sd, subsonic)
            if sub:
                return sub
        uri = getattr(media, "uri", None) if media else None
        if isinstance(uri, str) and uri.startswith(("http://", "https://")):
            return uri
        # Auto-discover a decodable URL MA exposes elsewhere (provider stream
        # URL not on .path, e.g. OpenSubsonic). Skip the album-art image url.
        art = getattr(media, "image_url", None) if media else None
        for obj in (sd, item, media):
            for u in iter_http_urls(obj):
                if u != art:
                    return u
    except Exception as err:  # noqa: BLE001 - defensive across MA versions
        _LOGGER.debug("[%s] map URL lookup failed: %s", entity_id, err)
    return None


def _ma_player_codec(player) -> str:
    """The output codec a Music Assistant player is configured to stream.

    Defaults to flac (MA's default). Strips any PCM parameter suffix and falls
    back gracefully across MA versions / config shapes.
    """
    try:
        config = getattr(player, "config", None)
        if config is not None and hasattr(config, "get_value"):
            codec = config.get_value("output_codec", default="flac")
            if codec:
                return str(codec).split(";", 1)[0].strip() or "flac"
    except Exception:  # noqa: BLE001 - defensive across MA versions
        pass
    return "flac"


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
        _LOGGER.debug("ffmpeg decode: %s -i %s (ss=%.2f)", self._ffmpeg, url, offset)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _LOGGER.error("ffmpeg binary %r not found; cannot decode audio", self._ffmpeg)
            self._proc = None

    async def _log_stderr_if_failed(self) -> None:
        """If ffmpeg exited immediately, surface its error output."""
        proc = self._proc
        if proc is None or proc.returncode is None or proc.stderr is None:
            return
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        if err:
            _LOGGER.warning("ffmpeg could not open the stream: %s", err.splitlines()[-1])

    async def read_hop(self) -> np.ndarray | None:
        """Read one hop of float32 samples (-1..1), or None at end of stream."""
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            raw = await self._proc.stdout.readexactly(_HOP_BYTES)
        except asyncio.IncompleteReadError as err:
            raw = err.partial
            if len(raw) < _HOP_BYTES:
                await self._log_stderr_if_failed()
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
        self._analyzer = Analyzer()
        # Remember the MA stream variant (flow/single + codec) that decoded, so a
        # resync on the next track rebuilds the same working URL shape.
        self._ma_kind: str | None = None
        self._ma_fmt: str | None = None
        self._cand_meta: dict[str, tuple[str, str]] = {}

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def album_art_url(self) -> str | None:
        return self._album_art_url

    @property
    def track_id(self) -> str | None:
        return self._track_id

    async def read_frame(self) -> AnalysisFrame | None:
        """Read the next paced hop and turn it into analysis features."""
        hop = await self.read_hop()
        if hop is None:
            return None
        return self._analyzer.push(hop)

    def _mass_player_id(self) -> str | None:
        """The MA player id is the HA entity's unique id."""
        entry = er.async_get(self._hass).async_get(self._entity_id)
        return entry.unique_id if entry else None

    def _mass_client(self):
        """Best-effort lookup of the Music Assistant client (cached ~30s)."""
        now = time.monotonic()
        if self._mass_cache is not None and now - self._mass_cache_ts < 30.0:
            return self._mass_cache
        self._mass_cache = _find_mass_client(self._hass)
        self._mass_cache_ts = now
        return self._mass_cache

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
        """Resolve the current track's tappable stream URL + position.

        Preference order for a decodable HTTP URL:
          1. ``player.current_media.uri`` (MA's re-encoded stream — universal).
          2. the active queue item's ``streamdetails.path`` (works for local /
             URL / radio providers).
          3. the HA ``media_content_id`` if it happens to be HTTP.
        """
        position, playing = self._ha_position()
        if not playing:
            _LOGGER.debug("[%s] not playing; no audio source", self._entity_id)
            return None

        state = self._hass.states.get(self._entity_id)
        attrs = state.attributes if state else {}
        is_live = attrs.get("media_duration") in (None, 0)

        stream_url: str | None = None
        alt_urls: list[str] = []
        source_desc = "none"
        album_art: str | None = None
        track_id: str | None = attrs.get("media_content_id")

        mass = self._mass_client()
        player_id = self._mass_player_id()
        # Captured for the failure WARNING so a single log line shows exactly
        # what Music Assistant exposed (no need to enable debug logging).
        diag: dict[str, object] = {"mass": mass is not None, "player_id": player_id}
        _LOGGER.debug(
            "[%s] resolve: mass_client=%s player_id=%s",
            self._entity_id, "found" if mass else "MISSING", player_id,
        )
        if mass is not None and player_id is not None:
            try:
                player = mass.players.get(player_id)
                media = getattr(player, "current_media", None)
                uri = getattr(media, "uri", None) if media else None
                base_url = getattr(getattr(mass, "server_info", None), "base_url", None)
                custom = getattr(media, "custom_data", None) if media else None
                diag.update(
                    base_url=bool(base_url),
                    uri=uri,
                    source_id=getattr(media, "source_id", None) if media else None,
                    queue_item_id=getattr(media, "queue_item_id", None) if media else None,
                    custom_keys=sorted((custom or {}).keys()) if custom else None,
                    active_source=getattr(player, "active_source", None) if player else None,
                )
                _LOGGER.debug(
                    "[%s] player=%s server=%s current_media: uri=%r media_type=%r "
                    "source_id=%r queue_item_id=%r custom_data=%r",
                    self._entity_id, "found" if player else "None", base_url, uri,
                    getattr(media, "media_type", None) if media else None,
                    getattr(media, "source_id", None) if media else None,
                    getattr(media, "queue_item_id", None) if media else None,
                    getattr(media, "custom_data", None) if media else None,
                )
                candidates: list[str] = []
                if media is not None:
                    album_art = getattr(media, "image_url", None)
                    track_id = uri or track_id
                    if uri and uri.startswith(("http://", "https://")):
                        # FLOW_STREAM/announcement media already carry the http URL.
                        candidates.append(uri)
                    # MA's own stream URL(s) — covers snapcast/pipe/squeezelite and
                    # any player whose current_media.uri is a provider URI.
                    candidates += self._ma_stream_candidates(media, player, base_url, player_id)
                # Fallback: the queue item's provider stream path.
                if player is not None:
                    src = getattr(player, "active_source", None)
                    queue = mass.player_queues.get(src) if src else None
                    item = getattr(queue, "current_item", None)
                    sd = getattr(item, "streamdetails", None)
                    path = getattr(sd, "path", None)
                    diag.update(
                        sd_path=path,
                        sd_provider=getattr(sd, "provider", None) if sd else None,
                        sd_item_id=getattr(sd, "item_id", None) if sd else None,
                        candidates=len(candidates),
                    )
                    _LOGGER.debug("[%s] queue streamdetails.path=%r", self._entity_id, path)
                    if isinstance(path, str) and path.startswith(("http://", "https://")):
                        candidates.append(path)
                    # Auto-discover any decodable HTTP URL MA exposes elsewhere
                    # (the resolved provider stream URL isn't always on .path,
                    # e.g. OpenSubsonic). open() validates each by decoding, so a
                    # non-audio URL is simply skipped. Album art is excluded.
                    for obj in (sd, item, media):
                        for u in iter_http_urls(obj):
                            if u != album_art and u not in candidates:
                                candidates.append(u)
                    if not candidates:
                        # Nothing usable: capture the full object shapes so the
                        # failure WARNING reveals where a URL/session id lives.
                        diag["sd_attrs"] = attr_summary(sd)
                        diag["item_attrs"] = attr_summary(item)
                        diag["media_attrs"] = attr_summary(media)
                        diag["queue_attrs"] = attr_summary(queue)
                if candidates:
                    seen: set[str] = set()
                    candidates = [u for u in candidates if not (u in seen or seen.add(u))]
                    stream_url = candidates[0]
                    alt_urls = candidates[1:]
                    source_desc = "ma-stream"
            except Exception as err:  # noqa: BLE001 - defensive across MA versions
                _LOGGER.debug("[%s] MA client lookup failed: %s", self._entity_id, err)

        if album_art is None and attrs.get("entity_picture"):
            album_art = self._absolute_url(attrs["entity_picture"])
        if stream_url is None:
            content = attrs.get("media_content_id")
            if content and content.startswith(("http://", "https://")):
                stream_url, source_desc = content, "media_content_id"

        if not stream_url:
            _LOGGER.warning(
                "[%s] no live audio tap (media_content_id=%r). Music Assistant "
                "exposed: %s. Will use the offline track-map if the track is "
                "analysable (incl. a configured OpenSubsonic library), else a "
                "generic animation.",
                self._entity_id, attrs.get("media_content_id"), diag,
            )
            return None

        _LOGGER.debug(
            "[%s] resolved stream via %s: %s (+%d alt) (pos=%.1fs live=%s)",
            self._entity_id, source_desc, stream_url, len(alt_urls), position, is_live,
        )
        return TrackInfo(
            stream_url=stream_url,
            position=position,
            track_id=track_id or stream_url,
            album_art_url=album_art,
            is_live=bool(is_live),
            alt_urls=alt_urls,
        )

    def _ma_stream_candidates(self, media, player, base_url, player_id) -> list[str]:
        """Build MA stream URLs to try, best-guess first.

        Mirrors the server's ``resolve_stream_url``:
        ``{base}/{flow|single}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{codec}``

        The flow-vs-single choice is the *player's* flow mode (squeezelite and
        other gapless-incapable players stream the whole queue as one flow), and
        the extension is the player's configured output codec — not always flac.
        Because we can't always read those exactly across MA versions, we emit a
        small ordered set of variants and let the first one that decodes win.
        """
        if not base_url:
            return []
        custom = getattr(media, "custom_data", None) or {}
        session_id = custom.get("session_id")
        queue_id = getattr(media, "source_id", None)
        queue_item_id = getattr(media, "queue_item_id", None)
        if not (session_id and queue_id and queue_item_id):
            return []

        prefer = (self._ma_kind, self._ma_fmt) if self._ma_kind and self._ma_fmt else None
        variants = ma_stream_variants(
            base_url, session_id, queue_id, queue_item_id, player_id,
            flow_mode=bool(getattr(player, "flow_mode", False)),
            codec=_ma_player_codec(player),
            prefer=prefer,
        )
        self._cand_meta = {url: (kind, fmt) for kind, fmt, url in variants}
        urls = [url for _kind, _fmt, url in variants]
        _LOGGER.debug("[%s] MA stream candidates: %s", self._entity_id, urls)
        return urls

    def _absolute_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        try:
            base = self._hass.config.internal_url or self._hass.config.external_url or ""
        except Exception:  # noqa: BLE001
            base = ""
        return f"{base.rstrip('/')}{path}" if base else path

    async def open(self) -> bool:
        """Resolve, start decoding, and confirm real audio actually flows.

        Returning False (no URL, or the URL won't decode) lets the coordinator
        fall back to the metadata source.
        """
        info = self._resolve()
        if info is None:
            return False
        # Try the primary URL then each alternate until one actually decodes
        # (handles flow-vs-single and output-codec differences between players).
        for url in [info.stream_url, *info.alt_urls]:
            await self._begin(info, url)
            try:
                first = await asyncio.wait_for(self._decoder.read_hop(), timeout=5.0)
            except asyncio.TimeoutError:
                first = None
            if first is not None:
                meta = self._cand_meta.get(url)
                if meta is not None:
                    self._ma_kind, self._ma_fmt = meta  # reuse this variant on resync
                if url != info.stream_url:
                    _LOGGER.info("[%s] using alternate stream variant %s", self._entity_id, url)
                self._analyzer.push(first)
                self._frames_emitted += 1
                return True
            await self._decoder.stop()
        _LOGGER.warning(
            "[%s] resolved stream(s) but none decoded; falling back", self._entity_id
        )
        return False

    async def _begin(self, info: TrackInfo, url: str | None = None) -> None:
        url = url or info.stream_url
        self._track_id = info.track_id
        self._album_art_url = info.album_art_url
        self._is_live = info.is_live
        start = 0.0 if info.is_live else max(0.0, info.position + self._latency)
        self._decode_start_pos = start
        self._frames_emitted = 0
        self._wall0 = time.monotonic()
        self._last_resync_check = self._wall0
        self._analyzer.reset()
        await self._decoder.start(url, start)
        _LOGGER.info(
            "Music sync tapping %s audio: %s (from %.1fs)",
            self._entity_id, url, start,
        )

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
