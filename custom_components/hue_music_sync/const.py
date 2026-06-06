"""Constants for the Hue Music Sync integration."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "hue_music_sync"

# --- Config entry keys ---------------------------------------------------
CONF_BRIDGE_ID: Final = "bridge_id"
CONF_HOST: Final = "host"
CONF_APP_KEY: Final = "app_key"  # Hue "username" / application key
CONF_CLIENT_KEY: Final = "client_key"  # PSK for DTLS, hex string
CONF_AREAS: Final = "areas"  # list of enabled entertainment_configuration ids

# --- Per-area option keys ------------------------------------------------
CONF_MEDIA_PLAYER: Final = "media_player"
CONF_COLOR_SCHEME: Final = "color_scheme"
CONF_EFFECT_MODE: Final = "effect_mode"
CONF_LATENCY_MS: Final = "latency_ms"
CONF_INTENSITY: Final = "intensity"

# --- Defaults ------------------------------------------------------------
DEFAULT_LATENCY_MS: Final = 150
DEFAULT_INTENSITY: Final = 1.0
DEFAULT_STREAM_FPS: Final = 50  # Hue entertainment recommended max ~50-60 Hz
DEFAULT_NAME: Final = "hue_music_sync#ha"

# Hue entertainment streaming
HUE_DTLS_PORT: Final = 2100
HUE_STREAM_PROTOCOL: Final = b"HueStream"
HUE_STREAM_VERSION: Final = b"\x02\x00"
KEEPALIVE_INTERVAL: Final = 9.0  # bridge drops the channel after ~10s of silence

# --- Audio analysis ------------------------------------------------------
# Decode rate for ffmpeg PCM output. 22050 mono is plenty for beat/band work
# and keeps FFT windows cheap.
ANALYSIS_SAMPLE_RATE: Final = 22050
ANALYSIS_HOP: Final = 441  # ~20ms hop -> ~50 feature frames/sec at 22050 Hz
ANALYSIS_WINDOW: Final = 1024  # FFT window size (samples)

# Frequency band edges in Hz: (sub_bass, bass, low_mid, mid, high).
# Each tuple is (low, high). Used by the analyzer to bucket FFT energy.
BANDS: Final[dict[str, tuple[float, float]]] = {
    "sub_bass": (20.0, 60.0),
    "bass": (60.0, 250.0),
    "low_mid": (250.0, 800.0),
    "mid": (800.0, 2500.0),
    "high": (2500.0, 11000.0),
}


class ColorScheme(StrEnum):
    """Selectable color schemes."""

    ALBUM_ART = "album_art"
    WARM = "warm"
    COOL = "cool"
    NEON = "neon"
    PARTY = "party"
    MONO = "mono"
    RAINBOW = "rainbow"


class EffectMode(StrEnum):
    """Choreography modes mapping audio features to lights."""

    PULSE = "pulse"  # whole area pulses on the beat with palette color
    SPECTRUM = "spectrum"  # channels mapped to frequency bands
    WAVE = "wave"  # beat triggers a travelling wave across positions
    AMBIENT = "ambient"  # slow palette drift, gentle energy modulation


DEFAULT_COLOR_SCHEME: Final = ColorScheme.ALBUM_ART
DEFAULT_EFFECT_MODE: Final = EffectMode.SPECTRUM

PLATFORMS: Final = ["switch", "select", "number"]


def signal_area_update(area_id: str) -> str:
    """Dispatcher signal fired when an area's sync state changes."""
    return f"{DOMAIN}_area_update_{area_id}"
