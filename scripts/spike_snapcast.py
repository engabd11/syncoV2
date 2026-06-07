#!/usr/bin/env python3
"""Spike: tap live audio from a Snapcast server and confirm it decodes.

Connects as a Snapcast client to the stream server (TCP 1704), points its own
group at the currently-playing stream via the control API (TCP 1705), then pipes
the FLAC codec header + wire chunks through ffmpeg and prints the RMS level to
prove we're receiving real (non-silent) audio.

    python spike_snapcast.py --host 192.168.0.200 --seconds 10
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import types

import numpy as np

# Load the integration's real Analyzer (stub package + StrEnum shim for py<3.11).
if not hasattr(enum, "StrEnum"):
    class _S(str, enum.Enum):
        def __str__(self): return self.value
    enum.StrEnum = _S  # type: ignore[attr-defined]
_CC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components")
if "hue_music_sync" not in sys.modules:
    _pkg = types.ModuleType("hue_music_sync")
    _pkg.__path__ = [os.path.join(_CC, "hue_music_sync")]
    sys.modules["hue_music_sync"] = _pkg
from hue_music_sync.audio.analyzer import Analyzer  # noqa: E402
from hue_music_sync.const import ANALYSIS_HOP  # noqa: E402

CONTROL_PORT = 1705
STREAM_PORT = 1704
CLIENT_ID = "hue_music_sync"

# Snapcast message types
T_CODEC_HEADER = 1
T_WIRE_CHUNK = 2
T_SERVER_SETTINGS = 3
T_HELLO = 5

_BASE = struct.Struct("<HHHiiiiI")  # type,id,refersTo,sent.s,sent.us,recv.s,recv.us,size


def control(host: str, method: str, params=None) -> dict:
    s = socket.create_connection((host, CONTROL_PORT), timeout=6)
    req = {"id": 1, "jsonrpc": "2.0", "method": method}
    if params is not None:
        req["params"] = params
    s.sendall((json.dumps(req) + "\r\n").encode())
    s.settimeout(6)
    buf = b""
    while b"\n" not in buf:
        buf += s.recv(65536)
    s.close()
    return json.loads(buf.split(b"\n")[0].decode())


def playing_stream(host: str) -> str | None:
    st = control(host, "Server.GetStatus")["result"]["server"]
    for s in st.get("streams", []):
        if s.get("status") == "playing":
            return s["id"]
    return None


def our_group(host: str) -> str | None:
    st = control(host, "Server.GetStatus")["result"]["server"]
    for g in st.get("groups", []):
        for c in g.get("clients", []):
            if c.get("id") == CLIENT_ID:
                return g["id"]
    return None


def send_msg(sock, mtype: int, payload: bytes, mid: int = 0) -> None:
    now = time.time()
    sec, usec = int(now), int((now % 1) * 1e6)
    sock.sendall(_BASE.pack(mtype, mid, 0, sec, usec, 0, 0, len(payload)) + payload)


def recv_exactly(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("snapserver closed connection")
        buf += chunk
    return buf


def recv_msg(sock):
    head = recv_exactly(sock, _BASE.size)
    mtype, mid, refers, ss, su, rs, ru, size = _BASE.unpack(head)
    return mtype, recv_exactly(sock, size)


def parse_string(payload: bytes, off: int):
    (n,) = struct.unpack_from("<I", payload, off)
    off += 4
    return payload[off : off + n], off + n


def hello_payload(host_ip: str) -> bytes:
    j = json.dumps({
        "MAC": "00:00:00:00:00:99",
        "HostName": "hue-music-sync",
        "Version": "0.27.0",
        "ClientName": "Snapclient",
        "OS": "linux",
        "Arch": "x86_64",
        "Instance": 1,
        "ID": CLIENT_ID,
        "SnapStreamProtocolVersion": 2,
    }).encode()
    return struct.pack("<I", len(j)) + j


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True)
    p.add_argument("--seconds", type=float, default=10.0)
    a = p.parse_args()

    target = playing_stream(a.host)
    print("Currently playing stream:", target)

    sock = socket.create_connection((a.host, STREAM_PORT), timeout=8)
    send_msg(sock, T_HELLO, hello_payload(a.host))
    print("Sent Hello; waiting for codec header...")

    # Point our group at the playing stream once the server knows us.
    time.sleep(0.5)
    gid = our_group(a.host)
    if gid and target:
        control(a.host, "Group.SetStream", {"id": gid, "stream_id": target})
        print(f"Set our group {gid} -> stream {target!r}")
    else:
        print("WARN: could not find our group/stream (gid=%r target=%r)" % (gid, target))

    stats = {"samples": 0, "peak": 0.0, "sumsq": 0.0, "beats": 0, "bands": None, "frames": 0}
    analyzer = Analyzer()
    hop_bytes = ANALYSIS_HOP * 2

    def drain(stdout):
        while True:
            raw = stdout.read(hop_bytes)
            if len(raw) < hop_bytes:
                break
            a16 = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            stats["samples"] += a16.size
            stats["peak"] = max(stats["peak"], float(np.max(np.abs(a16))))
            stats["sumsq"] += float(np.sum(a16 * a16))
            f = analyzer.push(a16)
            stats["frames"] += 1
            stats["beats"] += int(f.beat)
            vals = np.array([f.bands[k] for k in sorted(f.bands)])
            stats["bands"] = vals if stats["bands"] is None else stats["bands"] + vals
            stats["_last_bpm"] = f.tempo_bpm

    ff = None
    drain_thread = None
    sock.settimeout(8)
    start = time.monotonic()
    try:
        while time.monotonic() - start < a.seconds:
            mtype, payload = recv_msg(sock)
            if mtype == T_CODEC_HEADER:
                codec, off = parse_string(payload, 0)
                header, off = parse_string(payload, off)
                print("Codec:", codec.decode(), "header bytes:", len(header))
                if ff:
                    ff.kill()
                ff = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", codec.decode(),
                     "-i", "pipe:0", "-ac", "1", "-ar", "22050", "-f", "s16le", "pipe:1"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                drain_thread = threading.Thread(target=drain, args=(ff.stdout,), daemon=True)
                drain_thread.start()
                ff.stdin.write(bytes(header))
                ff.stdin.flush()
            elif mtype == T_WIRE_CHUNK and ff:
                (sz,) = struct.unpack_from("<I", payload, 8)
                audio = payload[12 : 12 + sz]
                ff.stdin.write(audio)
                ff.stdin.flush()
    finally:
        if ff:
            try:
                ff.stdin.close()
            except Exception:
                pass
            if drain_thread:
                drain_thread.join(timeout=2)
            ff.kill()
        sock.close()
        n = stats["samples"]
        if n:
            rms = (stats["sumsq"] / n) ** 0.5
            secs = n / 22050
            print(f"\nDecoded {n} samples ({secs:.1f}s). peak={stats['peak']:.3f} rms={rms:.3f}")
            print("=> AUDIO IS LIVE" if stats["peak"] > 0.01 else "=> SILENT")
            if stats["frames"]:
                bands = stats["bands"] / stats["frames"]
                print(f"beats detected: {stats['beats']} (~{stats['beats']/secs*60:.0f}/min)  "
                      f"tempo est: {stats.get('_last_bpm')}")
                names = ["bass", "high", "low_mid", "mid", "sub_bass"]
                print("mean bands:", {n: round(float(v), 2) for n, v in zip(names, bands)})
        else:
            print("No PCM decoded.")


if __name__ == "__main__":
    main()
