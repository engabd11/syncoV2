#!/usr/bin/env python3
"""Spike: connect to Music Assistant's Sendspin server as a passive client.

Music Assistant runs a Sendspin server on ``ws://<ma-host>:8927/sendspin``. A
client that declares only ``metadata@v1`` receives track metadata and the
``progress`` anchor without ever becoming an audio output target -- which is
exactly what the light show needs: an exact, timestamped playhead instead of
Home Assistant's coarse ``media_position``.

This probe answers the questions the integration work depends on:

  1. Does MA accept a metadata-only client, and does it stay invisible as a
     player? (Check the MA UI while this runs.)
  2. Which group's metadata does such a client receive, and does it need the
     ``controller@v1`` role to select one? (Try ``--controller``.)
  3. How is a group identified, so a followed HA media_player can be matched to
     it? (Watch the ``group/update`` dumps.)
  4. What is the real ``server/state`` cadence and clock round-trip jitter?

Seek, skip and pause while it runs: every ``server/state`` prints how far the
computed position has drifted from the previous anchor's projection, which is
the number the whole design rests on.

    python spike_sendspin.py --host 192.168.0.50
    python spike_sendspin.py --url ws://192.168.0.50:8927/sendspin --seconds 120
    python spike_sendspin.py --host 192.168.0.50 --controller --record fixtures.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid

try:
    import aiohttp
except ImportError:  # pragma: no cover - operator feedback, not library code
    sys.exit("aiohttp is required: pip install aiohttp")

DEFAULT_PORT = 8927
DEFAULT_PATH = "/sendspin"

# Fast while the clock filter converges, then slow: the drift term holds
# accuracy between exchanges, so steady-state chatter can be tiny.
_TIME_FAST_S = 0.5
_TIME_FAST_N = 10
_TIME_SLOW_S = 5.0


def _now_us() -> int:
    """Local monotonic microseconds -- the client's clock in Sendspin terms."""
    return int(time.monotonic() * 1_000_000)


class Probe:
    def __init__(self, url: str, roles: list[str], record) -> None:
        self._url = url
        self._roles = roles
        self._record = record
        self._client_id = f"synco-spike-{uuid.uuid4().hex[:8]}"
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._exchanges = 0
        # Rolling clock estimate (plain NTP average -- the integration uses the
        # protocol's Kalman filter; here we only want to see the raw quality).
        self._offset_us: float | None = None
        self._errors_us: list[float] = []
        # Last progress anchor, for the drift readout.
        self._anchor: tuple[float, float, float] | None = None  # (pos_s, server_us, rate)
        self._t0 = time.monotonic()

    # -- plumbing ------------------------------------------------------------

    def _log(self, tag: str, text: str) -> None:
        print(f"[{time.monotonic() - self._t0:7.3f}] {tag:<14} {text}", flush=True)

    async def _send(self, mtype: str, payload: dict) -> None:
        msg = {"type": mtype, "payload": payload}
        if self._record is not None:
            self._record.write(json.dumps({"dir": "out", **msg}) + "\n")
        await self._ws.send_str(json.dumps(msg))

    # -- clock ---------------------------------------------------------------

    async def _time_loop(self) -> None:
        """Keep an offset from the server clock (client/time -> server/time)."""
        try:
            while True:
                await self._send("client/time", {"client_transmitted": _now_us()})
                self._exchanges += 1
                n = self._exchanges
                await asyncio.sleep(_TIME_FAST_S if n <= _TIME_FAST_N else _TIME_SLOW_S)
        except asyncio.CancelledError:
            pass

    def _on_time(self, payload: dict) -> None:
        t1 = float(payload.get("client_transmitted", 0))
        t2 = float(payload.get("server_received", 0))
        t3 = float(payload.get("server_transmitted", 0))
        t4 = float(_now_us())
        # NTP-style: offset is the average of the two one-way skews; max_error
        # is half the portion of the round trip we cannot attribute.
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        max_error = ((t4 - t1) - (t3 - t2)) / 2.0
        self._offset_us = offset
        self._errors_us.append(max_error)
        if self._exchanges <= _TIME_FAST_N or self._exchanges % 6 == 0:
            self._log(
                "server/time",
                f"offset={offset / 1000:+9.2f} ms  max_error={max_error / 1000:6.2f} ms "
                f"rtt={(t4 - t1) / 1000:6.2f} ms",
            )

    def _server_now_us(self) -> float | None:
        return None if self._offset_us is None else _now_us() + self._offset_us

    # -- metadata ------------------------------------------------------------

    def _on_state(self, payload: dict) -> None:
        meta = payload.get("metadata")
        if meta is None:
            self._log("server/state", f"(no metadata) {json.dumps(payload)[:160]}")
            return
        title = meta.get("title")
        artist = meta.get("artist")
        stamp = meta.get("timestamp")
        prog = meta.get("progress")
        if prog is None:
            self._log("server/state", f"{artist} - {title}  (no progress object)")
            return

        pos_s = float(prog.get("track_progress", 0)) / 1000.0
        dur_s = float(prog.get("track_duration", 0)) / 1000.0
        rate = float(prog.get("playback_speed", 1000)) / 1000.0

        # How far off the *previous* anchor's projection was: this is the error
        # the light show would have accumulated by trusting the old anchor.
        drift = ""
        server_now = self._server_now_us()
        if self._anchor is not None and server_now is not None and stamp is not None:
            prev_pos, prev_stamp, prev_rate = self._anchor
            projected = prev_pos + prev_rate * (float(stamp) - prev_stamp) / 1_000_000.0
            drift = f"  drift_vs_prev={(pos_s - projected) * 1000:+8.1f} ms"

        if stamp is not None:
            self._anchor = (pos_s, float(stamp), rate)

        age = ""
        if server_now is not None and stamp is not None:
            age = f"  anchor_age={(server_now - float(stamp)) / 1000:+7.1f} ms"

        self._log(
            "server/state",
            f"{artist} - {title}  pos={pos_s:7.2f}s/{dur_s:6.1f}s "
            f"rate={rate:.3f}{age}{drift}",
        )

    # -- run -----------------------------------------------------------------

    async def run(self, seconds: float) -> int:
        hello = {
            "client_id": self._client_id,
            "name": "Hue Synco (spike)",
            "version": 1,
            "supported_roles": self._roles,
            "device_info": {
                "product_name": "Hue Synco",
                "manufacturer": "synco",
                "software_version": "spike",
            },
        }
        async with aiohttp.ClientSession() as session:
            self._log("connect", self._url)
            async with session.ws_connect(self._url, heartbeat=30) as ws:
                self._ws = ws
                await self._send("client/hello", hello)
                self._log("client/hello", f"roles={self._roles} id={self._client_id}")

                timer: asyncio.Task | None = None
                deadline = time.monotonic() + seconds
                try:
                    while time.monotonic() < deadline:
                        remaining = deadline - time.monotonic()
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # We requested no binary roles; if any arrives, that
                            # itself is a finding worth seeing.
                            self._log(
                                "BINARY",
                                f"type_byte={msg.data[0] if msg.data else '?'} "
                                f"len={len(msg.data)}",
                            )
                            continue
                        if msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            self._log("closed", f"{msg.type.name} {msg.data!r}")
                            break
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            self._log("error", str(ws.exception()))
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue

                        try:
                            data = json.loads(msg.data)
                        except ValueError:
                            self._log("BAD JSON", msg.data[:200])
                            continue
                        if self._record is not None:
                            self._record.write(
                                json.dumps({"dir": "in", **data}) + "\n"
                            )
                        mtype = data.get("type", "?")
                        payload = data.get("payload") or {}

                        if mtype == "server/time":
                            self._on_time(payload)
                        elif mtype == "server/state":
                            self._on_state(payload)
                        elif mtype == "server/hello":
                            self._log("server/hello", json.dumps(payload))
                            # Only start the clock loop once the handshake is
                            # done -- the spec forbids other messages before it.
                            if timer is None:
                                timer = asyncio.ensure_future(self._time_loop())
                        else:
                            # stream/start, stream/clear, stream/end,
                            # group/update, server/command -- dump verbatim.
                            self._log(mtype, json.dumps(payload))
                finally:
                    if timer is not None:
                        timer.cancel()
                    await self._send("client/goodbye", {})

        if self._errors_us:
            errs = sorted(self._errors_us)
            self._log(
                "summary",
                f"{len(errs)} clock exchanges  max_error "
                f"min={errs[0] / 1000:.2f} median={errs[len(errs) // 2] / 1000:.2f} "
                f"max={errs[-1] / 1000:.2f} ms",
            )
        else:
            self._log("summary", "no clock exchanges completed")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", help="Music Assistant host running the Sendspin server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--url", help="full ws:// URL (overrides --host/--port/--path)")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument(
        "--controller",
        action="store_true",
        help="also declare controller@v1 (needed if metadata-only gets no group)",
    )
    ap.add_argument("--record", help="append every message to this .jsonl file")
    args = ap.parse_args()

    if not args.url and not args.host:
        ap.error("one of --host or --url is required")
    url = args.url or f"ws://{args.host}:{args.port}{args.path}"

    roles = ["metadata@v1"]
    if args.controller:
        roles.append("controller@v1")

    rec = open(args.record, "a", encoding="utf-8") if args.record else None
    try:
        return asyncio.run(Probe(url, roles, rec).run(args.seconds))
    except KeyboardInterrupt:
        return 0
    except aiohttp.ClientError as err:
        print(f"connection failed: {err}", file=sys.stderr)
        return 1
    finally:
        if rec is not None:
            rec.close()


if __name__ == "__main__":
    raise SystemExit(main())
