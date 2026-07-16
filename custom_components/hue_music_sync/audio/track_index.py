"""Persistent per-track analysis outcomes, shared by every TrackMapper.

The mapper's in-memory failure bookkeeping (``trackmap._Failure``) vanishes on
restart and is invisible across instances: the library pre-warm and each area's
coordinator run their own mapper, so a track that failed the sweep used to be
silently re-decoded (and re-failed) at playback, and the user could never see
WHICH songs failed or why. This index is the shared on-disk answer: one small
JSON file next to the ``.npz`` cache recording, per track, the human label
("Artist - Title"), the structured failure/ambient reason and the retry state.

Only problem tracks are stored (``failed`` / ``retrying`` / ``ambient``); a
fully usable map's presence on disk IS its record. Keyed by the same
``sha1(track signature)`` the ``.npz`` cache files use, so a record and its map
file always agree. HA-free and event-loop friendly: mutations are plain dict
updates, file IO runs in an executor, and writes are debounced + atomic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_FORMAT = 1

# Debounce between automatic flushes: a library sweep records hundreds of
# outcomes; rewriting the JSON for each would be pure churn.
_FLUSH_MIN_S = 30.0

# Disk-cache sizing: at least this many .npz files are always allowed, and a
# large library gets 1.25x headroom so playback-analysed one-offs (radio, ad
# hoc singles) never evict pre-warmed library maps.
_MIN_DISK_BUDGET = 2048
_DISK_HEADROOM = 1.25


def track_key(track_id: str) -> str:
    """The stable per-track key: sha1 of the signature (same as the .npz name)."""
    return hashlib.sha1(track_id.encode("utf-8")).hexdigest()


class TrackIndex:
    """Shared persistent record of per-track analysis outcomes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._tracks: dict[str, dict] = {}
        self._library_total = 0
        self._loaded = False
        self._dirty = False
        self._last_flush = 0.0
        self._flush_lock = asyncio.Lock()

    # -- loading -------------------------------------------------------------

    def load_sync(self) -> None:
        """Blocking read (call from an executor). Safe to call repeatedly."""
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("format") != _FORMAT:
            return
        tracks = data.get("tracks")
        if isinstance(tracks, dict):
            self._tracks = {
                str(k): v for k, v in tracks.items() if isinstance(v, dict)
            }
        try:
            self._library_total = int(data.get("library_total") or 0)
        except (TypeError, ValueError):
            self._library_total = 0

    async def ensure_loaded(self) -> None:
        """Idempotent async load (executor-side file read)."""
        if self._loaded:
            return
        await asyncio.get_running_loop().run_in_executor(None, self.load_sync)

    # -- records ---------------------------------------------------------------

    def get(self, key: str) -> dict | None:
        return self._tracks.get(key)

    def record(
        self,
        key: str,
        *,
        label: str | None,
        status: str,
        reason: str = "",
        attempts: int = 0,
        permanent: bool = False,
        bpm: float | None = None,
        confidence: float | None = None,
    ) -> None:
        """Store/replace the record for one track (status: failed|retrying|ambient)."""
        prev = self._tracks.get(key) or {}
        self._tracks[key] = {
            # Keep the best label we ever saw (a retry may lack one).
            "label": label or prev.get("label") or "",
            "status": status,
            "reason": reason[:300],
            "attempts": attempts,
            "permanent": bool(permanent),
            "updated": time.time(),
            "bpm": round(bpm, 1) if bpm else None,
            "confidence": round(confidence, 3) if confidence is not None else None,
        }
        self._dirty = True

    def clear(self, key: str) -> None:
        """Drop the record (the track produced a fully usable map)."""
        if self._tracks.pop(key, None) is not None:
            self._dirty = True

    def clear_failures(self) -> int:
        """Drop every failed/retrying record so a sweep re-analyses them."""
        gone = [k for k, v in self._tracks.items() if v.get("status") != "ambient"]
        for k in gone:
            del self._tracks[k]
        if gone:
            self._dirty = True
        return len(gone)

    def ambient_keys(self) -> list[str]:
        """Keys of every ambient (features-only) map, for a forced re-analysis."""
        return [k for k, v in self._tracks.items() if v.get("status") == "ambient"]

    def failed_entries(self, cap: int = 25) -> list[dict]:
        """The failed tracks as small dicts (label + reason), newest first."""
        failed = [
            v for v in self._tracks.values() if v.get("status") == "failed"
        ]
        failed.sort(key=lambda v: v.get("updated") or 0.0, reverse=True)
        return [
            {"label": v.get("label") or "?", "reason": (v.get("reason") or "")[:120]}
            for v in failed[:cap]
        ]

    def counts(self) -> dict:
        """{"failed": n, "ambient": n, "retrying": n} over the stored records."""
        out = {"failed": 0, "ambient": 0, "retrying": 0}
        for v in self._tracks.values():
            s = v.get("status")
            if s in out:
                out[s] += 1
        return out

    # -- library sizing --------------------------------------------------------

    @property
    def library_total(self) -> int:
        return self._library_total

    def set_library_total(self, n: int) -> None:
        n = max(0, int(n))
        if n != self._library_total:
            self._library_total = n
            self._dirty = True

    @property
    def disk_budget(self) -> int:
        """How many .npz map files the shared cache dir may hold.

        Sized to the enumerated library (with headroom) so pre-warming a large
        library can never churn against the prune, and playback one-offs can
        never evict pre-warmed maps.
        """
        return max(_MIN_DISK_BUDGET, math.ceil(self._library_total * _DISK_HEADROOM))

    # -- persistence -----------------------------------------------------------

    def _payload(self) -> dict:
        return {
            "format": _FORMAT,
            "updated": time.time(),
            "library_total": self._library_total,
            "tracks": self._tracks,
        }

    def _write_sync(self, payload: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            tmp.replace(self._path)
        except OSError:
            _LOGGER.debug("Track index write failed", exc_info=True)

    async def flush(self, force: bool = False) -> None:
        """Persist when dirty; debounced unless ``force`` (executor-side write)."""
        if not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._last_flush < _FLUSH_MIN_S:
            return
        async with self._flush_lock:
            if not self._dirty:
                return
            self._last_flush = time.monotonic()
            self._dirty = False
            payload = json.loads(json.dumps(self._payload()))  # snapshot
            await asyncio.get_running_loop().run_in_executor(
                None, self._write_sync, payload
            )

    def write_report_sync(self, path: str | Path) -> None:
        """Blocking (executor): the full uncapped problem-track report as JSON.

        Everything the capped sensor attribute cannot carry: every failed and
        ambient track with its label, reason, attempt count and timestamp —
        the user-facing answer to "which songs failed the analysis and why".
        """
        report = {
            "generated": time.time(),
            "library_total": self._library_total,
            "counts": self.counts(),
            "failed": [
                {"label": v.get("label") or "?", **{k2: v.get(k2) for k2 in
                 ("reason", "attempts", "permanent", "updated")}}
                for v in sorted(
                    (v for v in self._tracks.values() if v.get("status") == "failed"),
                    key=lambda v: v.get("label") or "",
                )
            ],
            "ambient": [
                {"label": v.get("label") or "?", "reason": v.get("reason"),
                 "bpm": v.get("bpm"), "confidence": v.get("confidence")}
                for v in sorted(
                    (v for v in self._tracks.values() if v.get("status") == "ambient"),
                    key=lambda v: v.get("label") or "",
                )
            ],
        }
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=1)
            tmp.replace(p)
        except OSError:
            _LOGGER.debug("Analysis report write failed", exc_info=True)
