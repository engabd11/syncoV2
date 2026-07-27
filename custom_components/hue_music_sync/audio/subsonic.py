"""OpenSubsonic/Navidrome REST URL building (token auth).

Two layers:

* :func:`subsonic_auth_params` / :func:`subsonic_rest_url` — the generic token
  auth + endpoint URL builder every Subsonic call shares.
* :func:`subsonic_stream_url` / :func:`subsonic_cover_art_url` — the two
  binary-returning endpoints (audio, cover image) that the analyser, the live
  tap and the ``<img>`` tag point straight at.

Token auth (``t = md5(password + salt)``, random ``salt``) means the password is
never placed on a URL. Pure (hashlib/secrets/urllib only), so the URL + auth
logic is unit-tested without a live server.
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


def normalise_base_url(base_url: str) -> str:
    """Strip a trailing slash and default a scheme-less host to **https**.

    The URL carries the username and a password-derived token+salt
    (offline-crackable if sniffed), so plain HTTP must be an explicit choice
    (``http://…`` in the option), never a silent downgrade.
    """
    base = base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base


def subsonic_auth_params(
    username: str,
    password: str,
    *,
    client: str = _CLIENT,
    api_version: str = _API_VERSION,
    salt: str | None = None,
) -> dict[str, str]:
    """The shared token-auth query params ``u,t,s,v,c`` (password never on the URL)."""
    salt = salt or secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()  # noqa: S324 - Subsonic spec
    return {"u": username, "t": token, "s": salt, "v": api_version, "c": client}


def subsonic_rest_url(
    base_url: str,
    endpoint: str,
    params: dict[str, object],
    username: str,
    password: str,
    *,
    client: str = _CLIENT,
    api_version: str = _API_VERSION,
    salt: str | None = None,
    fmt: str | None = "json",
) -> str:
    """Build a ``{base}/rest/{endpoint}.view?…`` URL with token auth.

    ``fmt`` appends ``f=json`` for API (JSON) calls; pass ``fmt=None`` for the
    binary endpoints (``stream``, ``getCoverArt``) so the server returns audio /
    an image rather than a JSON wrapper.
    """
    base = normalise_base_url(base_url)
    query: dict[str, object] = dict(params)
    query.update(
        subsonic_auth_params(
            username, password, client=client, api_version=api_version, salt=salt
        )
    )
    if fmt:
        query["f"] = fmt
    return f"{base}/rest/{endpoint}.view?{urlencode(query)}"


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
    """Build a Subsonic ``/rest/stream`` URL, or None if any field is missing.

    The server transcodes to its default format unless it serves raw; ffmpeg (or
    a native decoder) handles either way, so we do not force a ``format``.
    """
    if not (base_url and username and password and item_id):
        return None
    return subsonic_rest_url(
        base_url,
        "stream",
        {"id": item_id},
        username,
        password,
        client=client,
        api_version=api_version,
        salt=salt,
        fmt=None,
    )


def subsonic_cover_art_url(
    base_url: str,
    username: str,
    password: str,
    cover_id: str,
    *,
    size: int | None = None,
    client: str = _CLIENT,
    api_version: str = _API_VERSION,
    salt: str | None = None,
) -> str | None:
    """Build a Subsonic ``/rest/getCoverArt`` image URL, or None if unusable."""
    if not (base_url and username and password and cover_id):
        return None
    params: dict[str, object] = {"id": cover_id}
    if size:
        params["size"] = int(size)
    return subsonic_rest_url(
        base_url,
        "getCoverArt",
        params,
        username,
        password,
        client=client,
        api_version=api_version,
        salt=salt,
        fmt=None,
    )
