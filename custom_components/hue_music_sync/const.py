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
CONF_MODE: Final = "mode"
CONF_EFFECT: Final = "effect"
CONF_COLOUR: Final = "colour"
CONF_BRIGHTNESS: Final = "brightness"
CONF_MEDIA_PLAYER: Final = "media_player"
CONF_LATENCY_MS: Final = "latency_ms"
CONF_TIMING_MS: Final = "timing_ms"
CONF_SNAPSERVER_HOST: Final = "snapserver_host"

# --- Defaults ------------------------------------------------------------
DEFAULT_LATENCY_MS: Final = 150
DEFAULT_INTENSITY: Final = 1.0
DEFAULT_STREAM_FPS: Final = 40  # Hue sweet spot; bulbs update ~12.5Hz, bridge eases
DEFAULT_NAME: Final = "hue_music_sync#ha"

# Hue entertainment streaming
HUE_DTLS_PORT: Final = 2100
HUE_STREAM_PROTOCOL: Final = b"HueStream"
HUE_STREAM_VERSION: Final = b"\x02\x00"
KEEPALIVE_INTERVAL: Final = 9.0  # bridge drops the channel after ~10s of silence
# The Entertainment API accepts at most ~10 lights per UDP packet; larger areas
# (multiple lamps + gradient-strip segments) must be split across packets or the
# bridge can drop the over-stuffed frame.
MAX_CHANNELS_PER_PACKET: Final = 10

# --- Audio analysis ------------------------------------------------------
# Decode rate for ffmpeg PCM output. 22050 mono is plenty for beat/band work
# and keeps FFT windows cheap.
ANALYSIS_SAMPLE_RATE: Final = 22050
ANALYSIS_HOP: Final = 441  # ~20ms hop -> ~50 feature frames/sec at 22050 Hz
ANALYSIS_WINDOW: Final = 1024  # FFT window size (samples)
# Master noise gate (RMS of the decoded signal, full-scale ~1.0). Below this the
# frame is treated as silence and rests fully, so the per-band AGC never
# amplifies a near-silent noise floor up to full brightness. ~-54 dBFS, well
# below any real music but above codec/dither hiss and digital-silence gaps.
ANALYSIS_NOISE_FLOOR: Final = 2.0e-3

# Frequency band edges in Hz: (sub_bass, bass, low_mid, mid, high).
# Each tuple is (low, high). Used by the analyzer to bucket FFT energy.
BANDS: Final[dict[str, tuple[float, float]]] = {
    "sub_bass": (20.0, 60.0),
    "bass": (60.0, 250.0),
    "low_mid": (250.0, 800.0),
    "mid": (800.0, 2500.0),
    "high": (2500.0, 11000.0),
}


DEFAULT_BRIGHTNESS: Final = 1.0  # master brightness ceiling (0..1)
DEFAULT_TIMING_MS: Final = 0  # +ve delays lights, -ve advances (within buffer)
TIMING_BUFFER_MS: Final = 200  # baseline delay buffer enabling -ve offsets


class ColorScheme(StrEnum):
    """Selectable colour themes — smooth, harmonious palettes plus album art."""

    ALBUM_ART = "album_art"
    SUNSET = "sunset"
    OCEAN = "ocean"
    FOREST = "forest"
    LAVENDER = "lavender"
    EMBER = "ember"
    AURORA = "aurora"
    RAINBOW = "rainbow"  # full spectrum; hue steps on the beat
    # Philips Hue signature scenes
    TROPICAL = "tropical"
    SAVANNA = "savanna"
    BLOSSOM = "blossom"
    HONOLULU = "honolulu"
    GALAXY = "galaxy"


class SyncMode(StrEnum):
    """Samsung-style intensity ladder controlling behaviour only.

    Sets how reactive the lights are (dimming range, beat brightening, shimmer)
    *relative to* the separate master brightness — not the absolute level.
    Parameters per mode live in ``effects.modes.MODE_PARAMS``.
    """

    SUBTLE = "subtle"  # seamless: steady level, colour just flows/shifts smoothly
    MEDIUM = "medium"  # gentle: stays bright, sways softly with the music
    HIGH = "high"  # dims to ~30%, the kick sweeps the room as a wavefront
    INTENSE = "intense"  # full 0-100% dimming/brightening + shimmer
    EXTREME = "extreme"  # club: ~1% floor to full, hard beats + fast wavefronts


class SyncEffect(StrEnum):
    """The renderer/choreography style — orthogonal to intensity and colour.

    ``MUSIC`` is the default audio-reactive choreography (dim/brighten + colour
    shifting). Other effects swap the whole render path while still drawing their
    colours from the selected palette and their energy from the music.
    """

    MUSIC = "music"  # default beat/frequency choreography
    MOVIES = "movies"  # calm, non-distracting: brightness follows the soundtrack
    FIREWORKS = "fireworks"  # bursts ignite on big beats and fade out


DEFAULT_MODE: Final = SyncMode.HIGH
DEFAULT_EFFECT: Final = SyncEffect.MUSIC
DEFAULT_COLOUR: Final = ColorScheme.ALBUM_ART

PLATFORMS: Final = ["switch", "select", "number"]


def signal_area_update(area_id: str) -> str:
    """Dispatcher signal fired when an area's sync state changes."""
    return f"{DOMAIN}_area_update_{area_id}"
