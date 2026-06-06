#!/usr/bin/env python3
"""Milestone 1 spike: prove Hue Entertainment DTLS streaming end-to-end.

Self-contained (stdlib + the ``openssl`` CLI only) so it can be dropped onto the
Home Assistant host and run without installing the integration. It exercises the
exact transport the integration uses: pairing, CLIP v2 config discovery, the
``{"action":"start"}`` handover, and a HueStream v2 colour cycle over DTLS.

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
import json
import ssl
import subprocess
import sys
import time
import urllib.request

HUE_STREAM_PROTOCOL = b"HueStream"
HUE_STREAM_VERSION = b"\x02\x00"
CIPHER = "PSK-AES128-GCM-SHA256"
DTLS_PORT = 2100


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
    url = f"https://{host}/api"
    res = _request("POST", url, body={"devicetype": "hue_music_sync#spike", "generateclientkey": True})
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
    url = f"https://{host}/clip/v2/resource/entertainment_configuration"
    res = _request("GET", url, headers={"hue-application-key": app_key})
    for cfg in res.get("data", []):
        name = cfg.get("metadata", {}).get("name", "?")
        n = len(cfg.get("channels", []))
        print(f"{cfg['id']}  | {name!r}  | channels={n}  | status={cfg.get('status')}")


def set_action(host: str, app_key: str, config_id: str, action: str) -> None:
    url = f"https://{host}/clip/v2/resource/entertainment_configuration/{config_id}"
    _request("PUT", url, headers={"hue-application-key": app_key}, body={"action": action})


def build_frame(config_id: str, seq: int, channels) -> bytes:
    frame = bytearray()
    frame += HUE_STREAM_PROTOCOL
    frame += HUE_STREAM_VERSION
    frame.append(seq & 0xFF)
    frame += b"\x00\x00"  # reserved
    frame.append(0x00)  # RGB colourspace
    frame.append(0x00)  # reserved
    frame += config_id.encode("ascii")
    for cid, r, g, b in channels:
        frame.append(cid & 0xFF)
        frame += int(r * 65535).to_bytes(2, "big")
        frame += int(g * 65535).to_bytes(2, "big")
        frame += int(b * 65535).to_bytes(2, "big")
    return bytes(frame)


def run_cycle(host: str, app_key: str, client_key: str, config_id: str, seconds: float) -> None:
    # Discover channel ids.
    url = f"https://{host}/clip/v2/resource/entertainment_configuration/{config_id}"
    res = _request("GET", url, headers={"hue-application-key": app_key})
    cfg = res["data"][0]
    channel_ids = [ch["channel_id"] for ch in cfg["channels"]]
    print(f"Streaming to {len(channel_ids)} channels: {channel_ids}")

    set_action(host, app_key, config_id, "start")
    time.sleep(0.3)

    proc = subprocess.Popen(
        [
            "openssl", "s_client", "-dtls1_2", "-cipher", CIPHER,
            "-psk_identity", app_key, "-psk", client_key,
            "-connect", f"{host}:{DTLS_PORT}", "-quiet",
        ],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    time.sleep(1.5)
    if proc.poll() is not None:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
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
            beat = 0.5 + 0.5 * abs((t * 2) % 2 - 1)  # triangle pulse ~1Hz
            channels = []
            for i, cid in enumerate(channel_ids):
                hue = ((t * 0.1) + i / max(1, len(channel_ids))) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1.0, beat)
                channels.append((cid, r, g, b))
            seq = (seq + 1) & 0xFF
            proc.stdin.write(build_frame(config_id, seq, channels))
            proc.stdin.flush()
            time.sleep(1 / fps)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.stdin.close()
            proc.terminate()
        except Exception:
            pass
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
        return
    if a.list:
        if not a.app_key:
            p.error("--list requires --app-key")
        list_configs(a.host, a.app_key)
        return
    if not (a.app_key and a.client_key and a.config_id):
        p.error("streaming requires --app-key, --client-key and --config-id")
    run_cycle(a.host, a.app_key, a.client_key, a.config_id, a.seconds)


if __name__ == "__main__":
    main()
