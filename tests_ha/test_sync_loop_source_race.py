"""Switching the media player while sync runs must not crash the sync loop.

A player change resets the audio source from a separate (eager) task. That runs
while the loop is parked inside ``read_frame()``: the metadata source sleeps
there to pace frames and still returns a frame, so the loop wakes up with
``self._source`` gone. It used to read ``self._source.track_id`` anyway, raise
``AttributeError`` and land in the "Recovering sync loop" error path (issue #14).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.hue_music_sync.coordinator import AreaSettings, SyncSession
from custom_components.hue_music_sync.hue.bridge import (
    EntertainmentChannel,
    EntertainmentConfig,
)


async def test_player_switch_mid_frame_does_not_crash_the_loop(hass, caplog):
    # Plain players (no Music Assistant): the real source picker falls back to
    # the metadata source, as it did for the reporter.
    for player in ("media_player.a", "media_player.b"):
        hass.states.async_set(player, "playing", {"media_title": "Song", "media_artist": "Band"})
    session = SyncSession(
        hass, AsyncMock(), "192.0.2.1", "app-key", "00" * 16, "ffmpeg",
        EntertainmentConfig(
            id="0" * 36, name="Living Room", status="active",
            channels=[EntertainmentChannel(0, -0.5, 0.5, 0.0), EntertainmentChannel(1, 0.5, 0.5, 0.0)],
        ),
        AreaSettings(media_player="media_player.a"),
    )
    session._stream = MagicMock()  # no bridge: only the frame path is under test
    session._send_timed = AsyncMock()
    session._send_idle = AsyncMock()
    caplog.set_level(logging.ERROR)

    session._running = True
    task = asyncio.create_task(session._run())
    try:
        for player in ("media_player.b", "media_player.a", "media_player.b"):
            await asyncio.sleep(0.4)
            session.apply_settings(dataclasses.replace(session._settings, media_player=player))
        sent = session._send_timed.await_count
        await asyncio.sleep(3.0)  # past the 2 s reopen: the loop picks up the new player
    finally:
        session._running = False
        await asyncio.wait_for(task, 5)

    assert "Recovering sync loop" not in caplog.text, caplog.text
    assert session._source is not None and session._source.entity_id == "media_player.b"
    assert session._send_timed.await_count > sent  # still rendering after the switch
