"""Tap live audio from a Snapcast server (primary for snapcast-backed players).

For Music Assistant players that play through Snapcast, connecting as a Snapcast
client and pointing our own group at the player's stream gives us the exact,
beat-accurate audio of whatever is playing. FLAC chunks are decoded with ffmpeg
and analysed, producing the same :class:`AnalysisFrame` stream as the other
sources. The coordinator only offers this tap to snapcast-backed players (see
``source.is_snapcast_backed``): the resolver below deliberately falls back to
"the playing stream" when no stream id matches the player uid, which is right
for snapcast players with renamed streams but would hijack another room's audio
for a Sendspin/squeezelite/cast player.

The Snapcast binary protocol (validated against snapserver 0.35) and ffmpeg are
driven on worker threads; analysis frames are handed to the async ``read_frame``
via a bounded queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import socket
import struct
import subprocess
import threading
import time

from typing import TYPE_CHECKING

import numpy as np

from ..const import ANALYSIS_HOP
from .analyzer import AnalysisFrame, Analyzer

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

CONTROL_PORT = 1705
STREAM_PORT = 1704
CLIENT_ID = "hue_music_sync"
_HOP_BYTES = ANALYSIS_HOP * 2  # s16le mono

# Snapcast message types
_T_CODEC_HEADER = 1
_T_WIRE_CHUNK = 2
_T_SERVER_SETTINGS = 3
_T_HELLO = 5

# Until the server tells us otherwise, assume its default end-to-end buffer.
# Real clients play each chunk ``bufferMs`` after its server timestamp, so our
# tap (which decodes chunks on arrival) leads the audible sound by this much.
DEFAULT_BUFFER_MS = 1000

_BASE = struct.Struct("<HHHiiiiI")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_server_settings(payload: bytes) -> dict | None:
    """Decode a ServerSettings message payload (uint32-length-prefixed JSON)."""
    try:
        (n,) = struct.unpack_from("<I", payload, 0)
        return json.loads(payload[4 : 4 + n].decode())
    except (struct.error, ValueError, UnicodeDecodeError):
        return None


def _control(host: str, method: str, params=None, timeout: float = 5.0) -> dict:
    """One-shot JSON-RPC call to the snapserver control port."""
    s = socket.create_connection((host, CONTROL_PORT), timeout=timeout)
    try:
        req = {"id": 1, "jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        s.sendall((json.dumps(req) + "\r\n").encode())
        s.settimeout(timeout)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n")[0].decode())
    finally:
        s.close()


def resolve_stream(host: str, player_uid: str | None) -> str | None:
    """Pick the snapcast stream for the followed player (or the playing one)."""
    server = _control(host, "Server.GetStatus")["result"]["server"]
    streams = server.get("streams", [])
    want = _norm(player_uid) if player_uid else ""
    playing = None
    for st in streams:
        sid = st.get("id", "")
        if st.get("status") == "playing":
            playing = playing or sid
            if want and want in _norm(sid):
                return sid
    # Fall back: an exact id match regardless of status, else the active stream.
    # A Snapcast-backed player always has a *playing* stream (whose id may not
    # match the player's uid), so returning it is correct; a squeezelite/slimproto
    # player streams over slimproto, so there is no playing snapcast stream and
    # `playing` is None, letting the caller fall back to the MA tap.
    if want:
        for st in streams:
            if want in _norm(st.get("id", "")):
                return st["id"]
    return playing


class SnapcastSource:
    """Live audio tap from a Snapcast server for one followed player."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        snapserver_host: str,
        ffmpeg_bin: str,
        player_uid: str | None,
    ) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._host = snapserver_host
        self._ffmpeg = ffmpeg_bin
        self._player_uid = player_uid

        self._stop = threading.Event()
        self._frames: queue.Queue[AnalysisFrame] = queue.Queue(maxsize=12)
        self._feeder: threading.Thread | None = None
        self._analyzer = Analyzer()
        self._album_art_url: str | None = None
        self._track_id: str | None = None
        self._meta_ts = 0.0
        self._buffer_ms = DEFAULT_BUFFER_MS  # updated from ServerSettings

    @property
    def playback_lead_ms(self) -> int:
        """How far this tap's analysis runs ahead of the audible sound.

        Snapcast clients play each chunk ``bufferMs`` after its server
        timestamp; we decode chunks the moment they arrive, so our feature
        frames lead the speakers by the server's buffer.
        """
        return self._buffer_ms

    @property
    def entity_id(self) -> str:
        return self._entity_id

    @property
    def album_art_url(self) -> str | None:
        return self._album_art_url

    @property
    def track_id(self) -> str | None:
        return self._track_id

    def _refresh_meta(self) -> None:
        st = self._hass.states.get(self._entity_id)
        if st is None:
            return
        attrs = st.attributes
        # Build a track id that reliably changes per song. Some Music Assistant
        # players keep media_content_id constant across tracks (a continuous flow
        # stream), so fold in title + artist; otherwise the album-art colours
        # would never refresh until sync was restarted.
        signature = "|".join(
            str(attrs[k])
            for k in ("media_content_id", "media_artist", "media_title")
            if attrs.get(k)
        )
        self._track_id = signature or None
        pic = attrs.get("entity_picture")
        if pic:
            if pic.startswith(("http://", "https://")):
                self._album_art_url = pic
            else:
                base = self._hass.config.internal_url or self._hass.config.external_url or ""
                self._album_art_url = f"{base.rstrip('/')}{pic}" if base else pic

    async def open(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            target = await loop.run_in_executor(
                None, resolve_stream, self._host, self._player_uid
            )
        except OSError as err:
            _LOGGER.debug("Snapserver %s unreachable: %s", self._host, err)
            return False
        if not target:
            _LOGGER.debug("No snapcast stream playing for %s", self._entity_id)
            return False

        self._refresh_meta()
        self._stop.clear()
        self._feeder = threading.Thread(
            target=self._run, args=(target,), name="hue_music_sync_snapcast", daemon=True
        )
        self._feeder.start()

        # Confirm audio actually flows before committing to this source.
        try:
            frame = await loop.run_in_executor(None, self._frames.get, True, 6.0)
        except queue.Empty:
            _LOGGER.warning("Snapcast connected but no audio decoded; falling back")
            await self.close()
            return False
        if not self._frames.full():
            self._frames.put_nowait(frame)  # re-queue the confirmation frame
        _LOGGER.info("Music sync tapping Snapcast stream %r for %s", target, self._entity_id)
        return True

    async def read_frame(self) -> AnalysisFrame | None:
        if self._stop.is_set():
            return None
        # Keep track/album-art current so colours follow track changes.
        if time.monotonic() - self._meta_ts > 1.0:
            self._meta_ts = time.monotonic()
            self._refresh_meta()
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._frames.get, True, 1.0)
        except queue.Empty:
            return None if self._stop.is_set() else AnalysisFrame(
                bands={}, energy=0.0
            )

    # -- worker threads ---------------------------------------------------

    def _run(self, target: str) -> None:
        """Feeder thread: snapcast socket -> ffmpeg stdin; spawns the decoder."""
        sock = None
        ff = None
        try:
            sock = socket.create_connection((self._host, STREAM_PORT), timeout=8)
            self._send(sock, _T_HELLO, self._hello())
            time.sleep(0.4)
            self._point_group(target)

            header_bytes: bytes | None = None
            decoder: threading.Thread | None = None
            sock.settimeout(10)
            while not self._stop.is_set():
                mtype, payload = self._recv(sock)
                if mtype == _T_CODEC_HEADER:
                    codec, off = self._read_str(payload, 0)
                    hdr, off = self._read_str(payload, off)
                    if header_bytes is None:
                        header_bytes = bytes(hdr)
                        # Stereo decode so pan (L/R spatial mapping) rides
                        # alongside the mono mid analysis (see _decode).
                        ff = subprocess.Popen(
                            [self._ffmpeg, "-nostdin", "-loglevel", "error",
                             "-f", codec.decode(), "-i", "pipe:0",
                             "-ac", "2", "-ar", "22050", "-f", "s16le", "pipe:1"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                        )
                        ff.stdin.write(header_bytes)
                        ff.stdin.flush()
                        decoder = threading.Thread(
                            target=self._decode, args=(ff,), daemon=True
                        )
                        decoder.start()
                    elif bytes(hdr) != header_bytes:
                        break  # stream/format changed -> let coordinator reopen
                elif mtype == _T_SERVER_SETTINGS:
                    settings = parse_server_settings(payload)
                    if settings and "bufferMs" in settings:
                        try:
                            self._buffer_ms = int(settings["bufferMs"])
                            _LOGGER.debug(
                                "Snapserver buffer for %s: %d ms",
                                self._entity_id, self._buffer_ms,
                            )
                        except (TypeError, ValueError):
                            pass
                elif mtype == _T_WIRE_CHUNK and ff is not None:
                    (sz,) = struct.unpack_from("<I", payload, 8)
                    try:
                        ff.stdin.write(payload[12 : 12 + sz])
                        ff.stdin.flush()
                    except (BrokenPipeError, OSError):
                        break
        except (OSError, ConnectionError) as err:
            _LOGGER.debug("Snapcast worker ended for %s: %s", self._entity_id, err)
        finally:
            self._stop.set()
            if ff is not None:
                try:
                    ff.stdin.close()
                except OSError:
                    pass
                ff.kill()
            if sock is not None:
                sock.close()

    def _decode(self, ff: subprocess.Popen) -> None:
        """Decoder thread: ffmpeg stereo PCM -> analysis frames -> queue."""
        need = _HOP_BYTES * 2  # interleaved stereo s16le
        while not self._stop.is_set():
            raw = ff.stdout.read(need)
            if len(raw) < need:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            frame = self._analyzer.push_stereo(samples[0::2], samples[1::2])
            if self._frames.full():
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                pass

    def _point_group(self, target: str) -> None:
        try:
            server = _control(self._host, "Server.GetStatus")["result"]["server"]
            for g in server.get("groups", []):
                if any(c.get("id") == CLIENT_ID for c in g.get("clients", [])):
                    _control(self._host, "Group.SetStream",
                             {"id": g["id"], "stream_id": target})
                    return
        except (OSError, KeyError) as err:
            _LOGGER.debug("Could not point snapcast group: %s", err)

    def _hello(self) -> bytes:
        j = json.dumps({
            "MAC": "00:00:00:00:00:99", "HostName": "hue-music-sync",
            "Version": "0.27.0", "ClientName": "Snapclient", "OS": "linux",
            "Arch": "x86_64", "Instance": 1, "ID": CLIENT_ID,
            "SnapStreamProtocolVersion": 2,
        }).encode()
        return struct.pack("<I", len(j)) + j

    @staticmethod
    def _send(sock, mtype: int, payload: bytes) -> None:
        now = time.time()
        sock.sendall(
            _BASE.pack(mtype, 0, 0, int(now), int((now % 1) * 1e6), 0, 0, len(payload))
            + payload
        )

    @staticmethod
    def _recv(sock):
        head = b""
        while len(head) < _BASE.size:
            chunk = sock.recv(_BASE.size - len(head))
            if not chunk:
                raise ConnectionError("snapserver closed")
            head += chunk
        size = _BASE.unpack(head)[7]
        body = b""
        while len(body) < size:
            chunk = sock.recv(size - len(body))
            if not chunk:
                raise ConnectionError("snapserver closed")
            body += chunk
        return _BASE.unpack(head)[0], body

    @staticmethod
    def _read_str(payload: bytes, off: int):
        (n,) = struct.unpack_from("<I", payload, off)
        off += 4
        return payload[off : off + n], off + n

    async def close(self) -> None:
        self._stop.set()
        feeder = self._feeder
        self._feeder = None
        if feeder is not None:
            await asyncio.get_running_loop().run_in_executor(None, feeder.join, 2.0)
