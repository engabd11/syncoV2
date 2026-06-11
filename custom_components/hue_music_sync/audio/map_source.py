"""Universal audio source: replay the offline track map for ANY player.

Snapcast and the Music Assistant HTTP tap give us real audio, but most player
types (AirPlay, Chromecast, Sonos, DLNA, ESPHome, groups, ...) expose no stream
we can tap without interfering with playback. This source needs none: it
*replays the precomputed per-frame analysis* (:class:`~.trackmap.TrackFeatures`)
locked to the player's reported position — the same trick the Hue×Spotify
integration uses. The runtime cost is an array lookup per frame (cheaper than a
live ffmpeg decode), and beats come from the offline DP tracker, so universal
support arrives with *better* timing, not worse.

A position PLL smooths the player's coarse position reports: an internal clock
free-runs at 1.0× and is gently pulled toward the reported position, snapping
only on real seeks. Frames are generated slightly *ahead* of the audible
position so the standard delay buffer + light pipeline land photons on the
beat.

While the map for the current track is still being analysed (a few seconds),
gentle metadata-style frames keep the lights alive; the source upgrades itself
the moment the map is cached.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from ..const import ANALYSIS_HOP, ANALYSIS_SAMPLE_RATE, BANDS, LIGHT_PIPELINE_MS, TIMING_BUFFER_MS
from .analyzer import AnalysisFrame
from .source import resolve_map_url

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .trackmap import TrackMapper

_LOGGER = logging.getLogger(__name__)

_FRAME_PERIOD = ANALYSIS_HOP / ANALYSIS_SAMPLE_RATE

# Generate frames this far ahead of the audible position: the coordinator's
# delay buffer holds them for (lead - LIGHT_PIPELINE_MS), and the light
# pipeline spends the rest, so the photons land on the beat with the usual
# +-TIMING_BUFFER_MS user trim available.
_AHEAD_MS = TIMING_BUFFER_MS + LIGHT_PIPELINE_MS

# Position PLL: gentle pull toward the reported position on each poll; a
# larger error than the snap threshold is a seek and re-anchors immediately.
_POS_GAIN = 0.15
_POS_SNAP_S = 0.40
_POS_POLL_S = 0.5


class TrackMapSource:
    """Analysis-frame playback for players with no tappable audio stream."""

    def __init__(self, hass: HomeAssistant, entity_id: str, mapper: TrackMapper) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._mapper = mapper
        self._track_id: str | None = None
        self._album_art_url: str | None = None
        self._pos = 0.0  # smoothed audible position (PLL clock)
        self._prev_query: float | None = None  # last frame_at position queried
        self._frames = 0
        self._wall0 = 0.0
        self._last_poll = 0.0
        self._last_meta = 0.0

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def album_art_url(self) -> str | None:
        return self._album_art_url

    @property
    def track_id(self) -> str | None:
        return self._track_id

    @property
    def playback_lead_ms(self) -> int:
        """Frames are generated this far ahead of the audible sound."""
        return _AHEAD_MS

    @property
    def analysis_position(self) -> float:
        """The track position the current analysis frames correspond to."""
        return self._pos + _AHEAD_MS / 1000.0

    # -- player state -------------------------------------------------------

    def _state(self):
        return self._hass.states.get(self._entity_id)

    def _reported_position(self) -> float | None:
        st = self._state()
        if st is None or st.state not in ("playing", "paused"):
            return None
        pos = st.attributes.get("media_position")
        if pos is None:
            return None
        live = float(pos)
        updated = st.attributes.get("media_position_updated_at")
        if st.state == "playing" and updated is not None:
            try:
                live += max(0.0, (dt_util.utcnow() - updated).total_seconds())
            except (TypeError, ValueError):
                pass
        return live

    def _refresh_meta(self) -> None:
        st = self._state()
        if st is None:
            return
        attrs = st.attributes
        # Per-song signature (media_content_id alone can stay constant on flow
        # streams) — must match what the coordinator keys the mapper with.
        signature = "|".join(
            str(attrs[k])
            for k in ("media_content_id", "media_artist", "media_title")
            if attrs.get(k)
        )
        track = signature or None
        if track != self._track_id:
            self._track_id = track
            self._prev_query = None
            self._ensure_map()
        pic = attrs.get("entity_picture")
        if pic:
            if pic.startswith(("http://", "https://")):
                self._album_art_url = pic
            else:
                base = self._hass.config.internal_url or self._hass.config.external_url or ""
                self._album_art_url = f"{base.rstrip('/')}{pic}" if base else pic

    def _ensure_map(self) -> None:
        """Kick background analysis for the current track (cheap if cached)."""
        if not self._track_id:
            return
        url = resolve_map_url(self._hass, self._entity_id)
        if url:
            self._mapper.ensure(self._track_id, url)

    # -- source interface ----------------------------------------------------

    async def open(self) -> bool:
        """Usable when the player reports a position and a track URL resolves.

        The map itself need not be ready yet — analysis runs in the background
        while gentle placeholder frames keep the lights alive. Returns False
        when analysis already failed for this track, so the coordinator can
        fall through to the metadata source instead of looping.
        """
        st = self._state()
        if st is None or st.state != "playing":
            return False
        if st.attributes.get("media_position") is None:
            return False  # no position, nothing to lock to
        self._refresh_meta()
        if not self._track_id:
            return False
        if self._mapper.failed(self._track_id):
            return False  # analysis already tried and failed for this track
        if self._mapper.get(self._track_id) is None:
            url = resolve_map_url(self._hass, self._entity_id)
            if url is None:
                return False  # radio/flow stream: nothing analysable per-track
            self._mapper.ensure(self._track_id, url)
        pos = self._reported_position()
        self._pos = pos if pos is not None else 0.0
        self._prev_query = None
        self._frames = 0
        self._wall0 = time.monotonic()
        _LOGGER.info(
            "Music sync using offline track-map playback for %s", self._entity_id
        )
        return True

    async def read_frame(self) -> AnalysisFrame | None:
        st = self._state()
        if st is None or st.state != "playing":
            return None
        # Pace to wall clock (deadline-based, like the live sources).
        self._frames += 1
        target = self._wall0 + self._frames * _FRAME_PERIOD
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        now = time.monotonic()
        if now - self._last_meta >= 1.0:
            self._last_meta = now
            self._refresh_meta()

        # Advance the PLL clock; periodically pull it toward the reported
        # position (gently, so coarse position reports can't cause stutter).
        self._pos += _FRAME_PERIOD
        if now - self._last_poll >= _POS_POLL_S:
            self._last_poll = now
            reported = self._reported_position()
            if reported is not None:
                err = reported - self._pos
                if abs(err) > _POS_SNAP_S:  # seek / gross drift: re-anchor
                    self._pos = reported
                    self._prev_query = None
                else:
                    self._pos += _POS_GAIN * err

        query = self._pos + _AHEAD_MS / 1000.0
        tm = self._mapper.get(self._track_id)
        frame = tm.frame_at(query, self._prev_query) if tm is not None else None
        self._prev_query = query
        if frame is not None:
            return frame
        # Map still analysing (or position past the analysed span): gentle
        # placeholder so the lights breathe instead of freezing.
        if self._mapper.failed(self._track_id):
            # Analysis failed for this track: tell the coordinator to fall
            # back (it will land on the metadata source).
            return None
        return self._placeholder_frame(query)

    def _placeholder_frame(self, t: float) -> AnalysisFrame:
        energy = 0.45 + 0.25 * math.sin(2 * math.pi * 0.12 * t)
        bands = {
            name: max(0.0, min(1.0, 0.35 + 0.3 * math.sin(2 * math.pi * (0.08 + i * 0.05) * t + i * 1.3)))
            for i, name in enumerate(BANDS)
        }
        return AnalysisFrame(bands=bands, energy=energy, t_audio=t)

    async def close(self) -> None:
        return
