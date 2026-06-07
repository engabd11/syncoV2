"""Metadata-only fallback source.

When no real audio is tappable (e.g. playing through a Snapcast/group player MA
won't expose a stream for), drive the lights from player *metadata* instead:
gentle LFO-shaped energy + soft periodic pulses, with colours from the current
album art. Not beat-accurate, but pleasant and universal — it reacts to
play/pause, track changes and the chosen mode/colour. Mirrors the
``MusicAssistantSource`` interface (``open`` / ``read_frame`` / ``close``).
"""

from __future__ import annotations

import asyncio
import math
import time

from homeassistant.core import HomeAssistant

from ..const import BANDS
from .analyzer import AnalysisFrame

_FPS = 40
_PERIOD = 1.0 / _FPS
_PULSE_HZ = 100.0 / 60.0  # soft pulses at ~100 BPM


class MetadataSource:
    """Synthesises analysis frames from media-player metadata."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._t0 = time.monotonic()
        self._frames = 0
        self._album_art_url: str | None = None
        self._track_id: str | None = None
        self._last_pulse = -1

    @property
    def album_art_url(self) -> str | None:
        return self._album_art_url

    @property
    def track_id(self) -> str | None:
        return self._track_id

    def _state(self):
        return self._hass.states.get(self._entity_id)

    def _playing(self) -> bool:
        st = self._state()
        return st is not None and st.state == "playing"

    def _refresh_meta(self) -> None:
        st = self._state()
        if st is None:
            return
        attrs = st.attributes
        self._track_id = attrs.get("media_content_id") or attrs.get("media_title")
        pic = attrs.get("entity_picture")
        if pic:
            self._album_art_url = self._absolute_url(pic)

    def _absolute_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        try:
            base = self._hass.config.internal_url or self._hass.config.external_url or ""
        except Exception:  # noqa: BLE001
            base = ""
        return f"{base.rstrip('/')}{path}" if base else path

    async def open(self) -> bool:
        if not self._playing():
            return False
        self._refresh_meta()
        self._t0 = time.monotonic()
        self._frames = 0
        return True

    async def read_frame(self) -> AnalysisFrame | None:
        if not self._playing():
            return None
        # Pace to wall clock (deadline-based, like the real source).
        self._frames += 1
        target = self._t0 + self._frames * _PERIOD
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._refresh_meta()

        t = time.monotonic() - self._t0
        # Slow breathing energy + per-band LFOs so spectrum modes vary by light.
        energy = 0.45 + 0.25 * math.sin(2 * math.pi * 0.12 * t)
        bands: dict[str, float] = {}
        for i, name in enumerate(BANDS):
            v = 0.35 + 0.3 * math.sin(2 * math.pi * (0.08 + i * 0.05) * t + i * 1.3)
            bands[name] = max(0.0, min(1.0, v))

        # Soft, even pulses so beat-reactive modes still move.
        beat = False
        strength = 0.0
        pulse_idx = int(t * _PULSE_HZ)
        if pulse_idx != self._last_pulse:
            self._last_pulse = pulse_idx
            beat = True
            strength = 0.6

        return AnalysisFrame(bands=bands, energy=energy, beat=beat, beat_strength=strength)

    async def close(self) -> None:
        return
