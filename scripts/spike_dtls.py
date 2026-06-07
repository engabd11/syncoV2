#!/usr/bin/env python3
"""Milestone 1 spike: prove Hue Entertainment DTLS streaming end-to-end.

Exercises the exact transport the integration uses: pairing, CLIP v2 config
discovery, the ``{"action":"start"}`` handover, and a HueStream v2 colour cycle
over the integration's pure-Python DTLS-PSK client (``hue/dtls.py``). Pairing and
discovery use only the stdlib; the streaming part needs ``cryptography`` (already
present in Home Assistant, and easy to ``pip install`` on a desktop on the same
LAN as the bridge).

Usage
-----
1. Press the bridge link button, then mint keys:
       python spike_dtls.py --host 192.168.1.10 --pair
2. List entertainment areas:
       python spike_dtls.py --host 192.168.1.10 --app-key KEY --list
3. Run a 15s colour cycle on an area:
       python spike_dtls.py --host 192.168.1.10 --app-key KEY \
           --client-key HEX --config-id UUID
"""

from __future__ import annotations

import argparse
import colorsys
import importlib.util
import json
import os
import ssl
import sys
import time
import urllib.request

DTLS_PORT = 2100


def _load_dtls_client():
    """Load DtlsPskClient straight from the integration source (no package import)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(
        os.path.dirname(here), "custom_components", "hue_music_sync", "hue", "dtls.py"
    )
    spec = importlib.util.spec_from_file_location("hms_dtls", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DtlsPskClient


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request(method: str, url: str, *, headers=None, body=None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ctx(), timeout=10) as resp:
        return json.loads(resp.read().decode())


def pair(host: str) -> None:
    res = _request(
        "POST", f"https://{host}/api",
        body={"devicetype": "hue_music_sync#spike", "generateclientkey": True},
    )
    entry = res[0]
    if "error" in entry:
        print("ERROR:", entry["error"].get("description"), file=sys.stderr)
        if entry["error"].get("type") == 101:
            print("Press the bridge link button, then re-run within 30s.", file=sys.stderr)
        sys.exit(1)
    s = entry["success"]
    print("app_key   :", s["username"])
    print("client_key:", s["clientkey"])


def list_configs(host: str, app_key: str) -> None:
    res = _request(
        "GET", f"https://{host}/clip/v2/resource/entertainment_configuration",
        headers={"hue-application-key": app_key},
    )
    for cfg in res.get("data", []):
        name = cfg.get("metadata", {}).get("name", "?")
        n = len(cfg.get("channels", []))
        print(f"{cfg['id']}  | {name!r}  | channels={n}  | status={cfg.get('status')}")


def set_action(host: str, app_key: str, config_id: str, action: str) -> None:
    _request(
        "PUT", f"https://{host}/clip/v2/resource/entertainment_configuration/{config_id}",
        headers={"hue-application-key": app_key}, body={"action": action},
    )


def build_frame(config_id: str, seq: int, channels) -> bytes:
    frame = bytearray(b"HueStream" + b"\x02\x00")
    frame.append(seq & 0xFF)
    frame += b"\x00\x00\x00\x00"  # reserved, RGB colourspace, reserved
    frame += config_id.encode("ascii")
    for cid, r, g, b in channels:
        frame.append(cid & 0xFF)
        frame += int(r * 65535).to_bytes(2, "big")
        frame += int(g * 65535).to_bytes(2, "big")
        frame += int(b * 65535).to_bytes(2, "big")
    return bytes(frame)


def run_cycle(host: str, app_key: str, client_key: str, config_id: str, seconds: float) -> None:
    DtlsPskClient = _load_dtls_client()

    res = _request(
        "GET", f"https://{host}/clip/v2/resource/entertainment_configuration/{config_id}",
        headers={"hue-application-key": app_key},
    )
    channel_ids = [ch["channel_id"] for ch in res["data"][0]["channels"]]
    print(f"Streaming to {len(channel_ids)} channels: {channel_ids}")

    set_action(host, app_key, config_id, "start")
    time.sleep(0.3)

    client = DtlsPskClient(host, DTLS_PORT, app_key.encode(), bytes.fromhex(client_key))
    print("Performing DTLS handshake...")
    try:
        client.connect()
    except Exception as err:  # noqa: BLE001
        print("DTLS handshake failed:", err, file=sys.stderr)
        set_action(host, app_key, config_id, "stop")
        sys.exit(1)

    print("DTLS up. Cycling colours (Ctrl+C to stop early)...")
    fps = 50
    start = time.monotonic()
    seq = 0
    try:
        while time.monotonic() - start < seconds:
            t = time.monotonic() - start
            beat = 0.5 + 0.5 * abs((t * 2) % 2 - 1)
            channels = []
            for i, cid in enumerate(channel_ids):
                hue = ((t * 0.1) + i / max(1, len(channel_ids))) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1.0, beat)
                channels.append((cid, r, g, b))
            seq = (seq + 1) & 0xFF
            client.send(build_frame(config_id, seq, channels))
            time.sleep(1 / fps)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        set_action(host, app_key, config_id, "stop")
        print("Stopped. Lights should restore to their previous state.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True)
    p.add_argument("--app-key")
    p.add_argument("--client-key")
    p.add_argument("--config-id")
    p.add_argument("--pair", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--seconds", type=float, default=15.0)
    a = p.parse_args()

    if a.pair:
        pair(a.host)
    elif a.list:
        if not a.app_key:
            p.error("--list requires --app-key")
        list_configs(a.host, a.app_key)
    elif a.app_key and a.client_key and a.config_id:
        run_cycle(a.host, a.app_key, a.client_key, a.config_id, a.seconds)
    else:
        p.error("streaming requires --app-key, --client-key and --config-id")


if __name__ == "__main__":
    main()
