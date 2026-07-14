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
from .library_match import LibraryEntry
from .source import ha_track_query, library_index, resolve_map_url, track_signature

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

    def __init__(
        self, hass: HomeAssistant, entity_id: str, mapper: TrackMapper, subsonic=None
    ) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._mapper = mapper
        self._subsonic = subsonic  # (url, user, password) for OpenSubsonic, or None
        self._lib = library_index(hass, subsonic)
        # The map key: the matched library track's signature once we know it
        # (see _apply_library_match), else the player's own signature.
        self._track_id: str | None = None
        self._ha_track: str | None = None  # what the player itself reports
        self._native_url: str | None = None  # per-track URL from MA, if it has one
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
        # streams) — shared with resolve_next_map so a prefetched map keys match.
        track = track_signature(
            attrs.get("media_content_id"),
            attrs.get("media_artist"),
            attrs.get("media_title"),
        )
        if track != self._ha_track:
            self._ha_track = track
            self._track_id = track
            self._prev_query = None
            # Re-anchor the replay clock to the new song immediately (it starts
            # near 0) so we don't replay the previous track's position into it.
            reported = self._reported_position()
            if reported is not None:
                self._pos = reported
            # Music Assistant's own per-track URL, if this player has one. When it
            # doesn't (a player MA doesn't own), matching the song in the MA
            # library is the only route to its audio — and the only players that
            # pay for that lookup are the ones that actually need it.
            self._native_url = resolve_map_url(self._hass, self._entity_id, self._subsonic)
            if self._native_url:
                self._mapper.ensure(self._track_id, self._native_url)
        if self._native_url is None and self._track_id == self._ha_track:
            # Not (yet) keyed to a library track: the background match may have
            # landed since the last poll, and this is only a dict lookup.
            self._apply_library_match()
        pic = attrs.get("entity_picture")
        if pic:
            if pic.startswith(("http://", "https://")):
                self._album_art_url = pic
            else:
                base = self._hass.config.internal_url or self._hass.config.external_url or ""
                self._album_art_url = f"{base.rstrip('/')}{pic}" if base else pic

    # -- library matching ----------------------------------------------------

    def _apply_library_match(self) -> LibraryEntry | None:
        """Key this song by the Music Assistant library track it *is*.

        The signature is the one the library pre-warm analysed under, so a song
        the "Analyse library" pass has already seen becomes a straight cache hit
        — whichever player is playing it. Sync and non-blocking: an unknown song
        only starts the lookup (see :meth:`MaLibraryIndex.lookup_soon`), and the
        alias lands on a later poll.
        """
        if not self._ha_track:
            return None
        entry = self._lib.lookup_soon(ha_track_query(self._hass, self._entity_id))
        if entry is not None:
            self._track_id = entry.signature
            self._mapper.ensure(entry.signature, entry.url)
        return entry

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
            url = self._native_url
            if url is None:
                # Music Assistant can't resolve audio for this player (it may not
                # even be an MA player — a Subsonic radio on the same Navidrome
                # library, say). The song's library match, once known, yields the
                # pre-warm's map key *and* a decodable URL, so the same library
                # track lights up the room whoever is playing it. Still matching?
                # Then there's nothing to play yet: the metadata animation covers
                # the song and the coordinator's probe re-opens us once it lands.
                entry = self._apply_library_match()
                if entry is not None:
                    url = entry.url
            # ensure_ready() loads a cached map from disk (awaited, off-loop) even
            # when ``url`` is None, so a previously-played track gets track-map
            # playback as a *single* track too (no queue / no fresh per-track URL
            # needed). Only give up when there is neither a cached map nor
            # anything analysable.
            tm = await self._mapper.ensure_ready(self._track_id, url)
            if tm is None and url is None:
                return False  # radio/flow & never analysed: nothing to play
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
        # Refresh on the same cadence as the position poll: on a gapless boundary
        # the track id changes here and re-anchors the clock, so the smaller this
        # is the shorter the window where the *previous* track's map is queried.
        if now - self._last_meta >= _POS_POLL_S:
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
