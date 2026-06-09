"""Pure helpers for building Music Assistant stream URLs (no HA imports).

Kept separate from :mod:`source` (which depends on Home Assistant) so the
URL-variant logic can be unit-tested on its own.
"""

from __future__ import annotations


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
