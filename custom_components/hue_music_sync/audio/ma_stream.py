"""Pure helpers for Music Assistant stream/player logic (no HA imports).

Kept separate from :mod:`source` (which depends on Home Assistant) so the
URL-variant and provider logic can be unit-tested on their own.
"""

from __future__ import annotations


def is_snapcast_backed(provider: str | None) -> bool:
    """Whether the snapcast tap may be used for a player of this MA provider.

    The snapserver's stream resolver has a deliberate fallback to "the playing
    stream" (MA's snapcast stream ids don't always contain the player uid), so
    pointing it at a non-snapcast player (Sendspin, squeezelite, AirPlay...)
    while *any* snapcast stream is live would sync the lights to the wrong
    room's audio. Unknown providers stay eligible so exotic MA versions where
    the lookup fails keep the legacy behaviour.
    """
    return provider is None or "snap" in provider.lower()


def ma_stream_variants(
    base_url: str | None,
    session_id: str | None,
    queue_id: str | None,
    queue_item_id: str | None,
    player_id: str | None,
    *,
    flow_mode: bool,
    codec: str,
    prefer: tuple[str, str] | None = None,
) -> list[tuple[str, str, str]]:
    """Ordered ``(kind, codec, url)`` MA stream variants to try, best first.

    Mirrors the server's ``resolve_stream_url``::

        {base}/{flow|single}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{codec}

    The flow-vs-single choice is the *player's* flow mode (squeezelite and other
    gapless-incapable players stream the whole queue as one flow), and the
    extension is the player's configured output codec — not always flac. We emit
    a small ordered set (player's best guess, then the other kind, each in the
    player's codec then flac) and let the first that decodes win. ``prefer`` puts
    a previously-working ``(kind, codec)`` first so a resync reuses it.
    """
    if not (base_url and session_id and queue_id and queue_item_id and player_id):
        return []
    primary = "flow" if flow_mode else "single"
    other = "single" if flow_mode else "flow"

    pairs: list[tuple[str, str]] = []
    if prefer is not None:
        pairs.append(prefer)
    for kind in (primary, other):
        for fmt in (codec, "flac"):
            if (kind, fmt) not in pairs:
                pairs.append((kind, fmt))

    base = base_url.rstrip("/")
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for kind, fmt in pairs:
        url = f"{base}/{kind}/{session_id}/{queue_id}/{queue_item_id}/{player_id}.{fmt}"
        if url not in seen:
            seen.add(url)
            out.append((kind, fmt, url))
    return out
