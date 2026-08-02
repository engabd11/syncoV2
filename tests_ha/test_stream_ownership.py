"""The rule that makes the Hue app's stop button work.

The bridge hands an entertainment area to exactly one application at a time.
When ours is taken away — the user pressed stop in the Hue app, another app
claimed the area, streaming was disabled via CLIP — the DTLS channel dies, and
that looks identical to a Wi-Fi hiccup from the socket's point of view. Telling
the two apart is the whole job of these tests: a revocation must end the session,
and a plain network drop must still reconnect.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.hue_music_sync.coordinator import SyncSession
from custom_components.hue_music_sync.hue.bridge import (
    EntertainmentConfig,
    HueBridgeError,
)

OUR_APP_ID = "ours-1234"
AREA = EntertainmentConfig(id="area-1", name="Living Room", status="active")


def _session(status="active", streamer=OUR_APP_ID, fail=False) -> SyncSession:
    """A SyncSession with only the ownership machinery wired up.

    Built without __init__ on purpose: the real constructor spins up the audio
    analyser, effect engine and track mapper, none of which this behaviour
    touches.
    """
    session = SyncSession.__new__(SyncSession)
    session._config = AREA
    session._app_id = OUR_APP_ID
    session._external_stop = False
    session._running = True
    session._stopping = False
    bridge = AsyncMock()
    if fail:
        bridge.get_entertainment_status = AsyncMock(
            side_effect=HueBridgeError("bridge unreachable")
        )
    else:
        bridge.get_entertainment_status = AsyncMock(return_value=(status, streamer))
    session._bridge = bridge
    return session


# --- ownership check ------------------------------------------------------

async def test_still_ours_while_the_area_is_active_and_ours():
    session = _session()
    assert await session._stream_still_ours() is True
    assert session.externally_stopped is False
    assert session._running is True


async def test_inactive_area_ends_the_session():
    # This is the reported bug: the Hue app stops the area, our next send fails,
    # and the old code answered by re-issuing action=start a second later.
    session = _session(status="inactive", streamer=None)
    assert await session._stream_still_ours() is False
    assert session.externally_stopped is True
    assert session._running is False


async def test_another_application_holding_the_area_ends_the_session():
    session = _session(status="active", streamer="some-other-app")
    assert await session._stream_still_ours() is False
    assert session.externally_stopped is True


async def test_a_failed_query_is_treated_as_still_ours():
    # An inconclusive answer must not turn a recoverable drop into a giving-up
    # session — the bridge being briefly unreachable is exactly the case the
    # reconnect loop exists for.
    session = _session(fail=True)
    assert await session._stream_still_ours() is True
    assert session.externally_stopped is False


async def test_missing_application_id_ignores_the_streamer_field():
    # On firmware without /auth/v1 we have no application id to compare
    # against, so active_streamer alone can't be read as a takeover.
    session = _session(status="active", streamer="anything")
    session._app_id = None
    assert await session._stream_still_ours() is True


# --- reconnect guard ------------------------------------------------------

async def test_reconnect_refuses_once_externally_stopped():
    session = _session()
    session.note_external_stop("close_notify")
    assert await session._reconnect_stream() is False
    # Never even asked the bridge — and crucially never sent action=start.
    session._bridge.get_entertainment_status.assert_not_awaited()
    session._bridge.start_stream.assert_not_awaited()


async def test_reconnect_aborts_without_issuing_start_when_area_was_stopped():
    session = _session(status="inactive", streamer=None)
    session._stream = AsyncMock()

    assert await session._reconnect_stream() is False
    session._bridge.start_stream.assert_not_awaited()
    assert session.externally_stopped is True


# --- event handling -------------------------------------------------------

def test_event_for_our_area_going_inactive_stops_the_session():
    session = _session()
    session.on_bridge_event(
        {"id": "area-1", "type": "entertainment_configuration", "status": "inactive"}
    )
    assert session.externally_stopped is True
    assert session._running is False


def test_event_naming_another_streamer_stops_the_session():
    session = _session()
    session.on_bridge_event(
        {"id": "area-1", "active_streamer": "some-other-app"}
    )
    assert session.externally_stopped is True


def test_event_confirming_our_own_stream_changes_nothing():
    session = _session()
    session.on_bridge_event(
        {"id": "area-1", "status": "active", "active_streamer": OUR_APP_ID}
    )
    assert session.externally_stopped is False
    assert session._running is True


def test_events_for_other_areas_are_ignored():
    session = _session()
    session.on_bridge_event({"id": "some-other-area", "status": "inactive"})
    assert session.externally_stopped is False


def test_partial_event_without_status_or_streamer_is_harmless():
    # Events carry only what changed; a rename must not stop the show.
    session = _session()
    session.on_bridge_event({"id": "area-1", "metadata": {"name": "Lounge"}})
    assert session.externally_stopped is False


def test_note_external_stop_is_idempotent():
    session = _session()
    session.note_external_stop("first")
    session._running = True  # pretend something restarted it
    session.note_external_stop("second")
    assert session.externally_stopped is True
    assert session._running is True  # the second call was a no-op
