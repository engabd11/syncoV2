"""Build a decodable stream URL for an OpenSubsonic/Subsonic (e.g. Navidrome) track.

When the player is fed by Music Assistant's OpenSubsonic provider, MA resolves
the actual audio URL server-side and never exposes it to us, and there is no
flow-stream session to reconstruct. But the queue item still tells us the
provider track id (``streamdetails.item_id``), so - given the server's URL and
login (an integration option) - we can build the standard Subsonic ``stream``
request ourselves and hand it straight to ffmpeg. ffmpeg decodes it directly
(the server is on the LAN), which both the live tap and the offline track-map
can use - sidestepping the missing MA session entirely.

Pure (hashlib/secrets/urllib only), so the URL + token-auth logic is unit-tested.
"""

from __future__ import annotations

import hashlib
import secrets
from urllib.parse import urlencode

# Subsonic API version we speak; 1.16.1 is widely supported (incl. Navidrome).
_API_VERSION = "1.16.1"
_CLIENT = "huesynco"


def is_subsonic_provider(provider: str | None) -> bool:
    """Whether an MA provider id is an (Open)Subsonic provider (e.g. Navidrome)."""
    return bool(provider) and "subsonic" in provider.lower()


def subsonic_stream_url(
    base_url: str,
    username: str,
    password: str,
    item_id: str,
    *,
    client: str = _CLIENT,
    api_version: str = _API_VERSION,
    salt: str | None = None,
) -> str | None:
    """Build a Subsonic ``/rest/stream`` URL with token auth, or None if unusable.

    Token auth (``t = md5(password + salt)``, random ``salt``) so the password is
    never put on the URL. Returns None when any required field is missing. The
    server transcodes to its default format unless it serves raw; ffmpeg decodes
    either way, so we do not force a ``format``.
    """
    if not (base_url and username and password and item_id):
        return None
    base = base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        # A scheme-less host defaults to httpS: the URL carries the username and
        # a password-derived token+salt (offline-crackable if sniffed), so plain
        # HTTP must be an explicit choice ("http://..." in the option), not a
        # silent downgrade.
        base = "https://" + base
    salt = salt or secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()  # noqa: S324 - Subsonic spec
    params = {
        "id": item_id,
        "u": username,
        "t": token,
        "s": salt,
        "v": api_version,
        "c": client,
    }
    return f"{base}/rest/stream.view?{urlencode(params)}"
