"""Small shared helpers."""

from __future__ import annotations

from urllib.parse import urlsplit


def redact_url(url: str | None) -> str:
    """Return ``url`` with its query string masked, for safe logging.

    Stream and artwork URLs can carry auth material (the Subsonic token+salt,
    signed-URL parameters), and users routinely paste DEBUG logs into bug
    reports — so never log a query string verbatim.
    """
    if not url:
        return str(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parts.query:
        return url
    base = url.split("?", 1)[0]
    return f"{base}?<redacted {len(parts.query)} chars>"
