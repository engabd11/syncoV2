"""Live WebSocket feed for the dashboard card.

The card subscribes with the area's sync switch entity id and receives two
event kinds while that area is streaming:

* ``stream`` (~20 Hz) — the real analysis driving the lights right now:
  normalised band energies, loudness, kick flags, the per-lamp colours being
  emitted (so the card can mirror the room), and the current instrument-role
  assignment.
* ``meta`` (~1 Hz) — the slow picture: lamp positions, the track map's section
  timeline, tempo, playback position/duration.

Pushed over the existing HA WebSocket — no state writes, nothing touches the
recorder, and nothing is sent at all unless at least one card is subscribed.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_WS_REGISTERED = "ws_registered"


def async_register_ws(hass: HomeAssistant) -> None:
    """Register the subscribe command once per HA instance."""
    if hass.data[DOMAIN].get(DATA_WS_REGISTERED):
        return
    hass.data[DOMAIN][DATA_WS_REGISTERED] = True
    websocket_api.async_register_command(hass, ws_subscribe)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/subscribe",
        vol.Required("entity_id"): str,
    }
)
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Subscribe a card to an area's live feed (keyed by its sync switch).

    Access scope (by design): any authenticated HA user may subscribe — the feed
    is intentionally available to non-admin dashboard users, since dashboards run
    under regular user sessions. The payload is low-sensitivity (per-lamp colours,
    lamp positions, tempo, and the now-playing title that the switch already
    exposes as state attributes), carries no credentials, and is read-only — it
    triggers no state writes and never touches the recorder. We therefore do not
    gate this with ``require_admin``; HA still requires a valid auth token.
    """
    from . import DATA_AREA_INDEX  # local import to avoid a setup cycle

    index = hass.data.get(DOMAIN, {}).get(DATA_AREA_INDEX, {})
    target = index.get(msg["entity_id"])
    if target is None:
        connection.send_error(
            msg["id"], "not_found", f"No sync switch {msg['entity_id']}"
        )
        return
    manager, area_id = target

    @callback
    def forward(payload: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], payload))

    connection.subscriptions[msg["id"]] = manager.ws_subscribe(area_id, forward)
    connection.send_result(msg["id"])
    # Immediate snapshot so a card joining mid-song paints at once.
    snapshot = manager.ws_snapshot(area_id)
    if snapshot:
        forward(snapshot)
