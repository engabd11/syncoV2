"""Pure helpers for Music Assistant stream/player logic (no HA imports).

Kept separate from :mod:`source` (which depends on Home Assistant) so the
URL-variant and provider logic can be unit-tested on their own.
"""

from __future__ import annotations

from .subsonic import is_subsonic_provider, subsonic_stream_url


def iter_http_urls(obj):
    """Yield http(s) URL strings found on an MA object's public attributes.

    Music Assistant exposes the resolved provider stream URL in different fields
    across providers/versions (for OpenSubsonic, for instance, it is not on
    ``streamdetails.path``). Rather than guess the field name, scan the object's
    attributes (and one level of nested dicts) and let the decode-probe in
    ``MusicAssistantSource.open()`` validate which URL is real, decodable audio.
    Pure (operates on any object via ``vars``), so it is unit-tested here.
    """
    if obj is None:
        return
    try:
        attrs = vars(obj)
    except TypeError:
        return
    for key, val in attrs.items():
        if key.startswith("_"):
            continue
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            yield val
        elif isinstance(val, dict):
            for vv in val.values():
                if isinstance(vv, str) and vv.startswith(("http://", "https://")):
                    yield vv


def mapping_stream_urls(media_item) -> list[str]:
    """Stream URLs a media item's provider mappings carry in ``details``.

    Saved TuneIn radio stations reach the client with ``streamdetails.path``
    unset; the station's stream URL is only on its provider mapping's
    ``details``. (``url`` there is the provider's web page, so it is skipped.)
    """
    mappings = getattr(media_item, "provider_mappings", None) or []
    return [
        d for pm in mappings
        if isinstance(d := getattr(pm, "details", None), str)
        and d.startswith(("http://", "https://"))
    ]


def library_track_url(track, subsonic) -> str | None:
    """A decodable URL for an MA library Track (for offline pre-analysis).

    Prefers an (Open)Subsonic provider mapping built into a ``/rest/stream`` URL
    (the reliable path for a Navidrome library), then any http URL the track's
    provider mappings expose. ``subsonic`` is ``(url, user, password)`` or None.
    Pure, so it is unit-tested.
    """
    mappings = getattr(track, "provider_mappings", None) or []
    for pm in mappings:
        prov = getattr(pm, "provider_domain", None) or getattr(pm, "provider_instance", None)
        if subsonic and is_subsonic_provider(prov):
            url = subsonic_stream_url(*subsonic, getattr(pm, "item_id", None))
            if url:
                return url
        u = getattr(pm, "url", None)
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return u
    for u in iter_http_urls(track):
        return u
    return None


def as_track_list(result) -> list:
    """Coerce an MA library response (a list, or a paged object) into a list."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    for attr in ("items", "tracks", "data"):
        val = getattr(result, attr, None)
        if isinstance(val, list):
            return val
    try:
        return list(result)
    except TypeError:
        return []


def attr_summary(obj) -> dict:
    """Compact ``{attr: short-repr}`` of an object's public attributes (for logs)."""
    if obj is None:
        return {}
    try:
        attrs = vars(obj)
    except TypeError:
        return {}
    out: dict[str, str] = {}
    for k, v in attrs.items():
        if k.startswith("_"):
            continue
        r = repr(v)
        out[k] = r if len(r) <= 160 else r[:160] + "..."
    return out


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
