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
# The bridge's self-signed TLS certificate (PEM), captured at pairing time
# (trust-on-first-use) so every later CLIP call verifies it is talking to the
# same bridge instead of accepting any certificate.
CONF_BRIDGE_CERT: Final = "bridge_certificate"

# --- Per-area option keys ------------------------------------------------
CONF_MODE: Final = "mode"
CONF_EFFECT: Final = "effect"
CONF_COLOUR: Final = "colour"
CONF_BRIGHTNESS: Final = "brightness"
CONF_MEDIA_PLAYER: Final = "media_player"
CONF_LATENCY_MS: Final = "latency_ms"
CONF_TIMING_MS: Final = "timing_ms"
CONF_SNAPSERVER_HOST: Final = "snapserver_host"
# OpenSubsonic / Navidrome library (optional): lets us fetch & analyse library
# tracks directly when Music Assistant won't expose a tappable stream URL
# (e.g. Sendspin playing an OpenSubsonic track).
CONF_SUBSONIC_URL: Final = "subsonic_url"
CONF_SUBSONIC_USER: Final = "subsonic_user"
CONF_SUBSONIC_PASSWORD: Final = "subsonic_password"
CONF_RESTORE_LIGHTS: Final = "restore_lights"  # snapshot + restore light state on stop

# --- Defaults ------------------------------------------------------------
DEFAULT_LATENCY_MS: Final = 150
DEFAULT_INTENSITY: Final = 1.0
DEFAULT_STREAM_FPS: Final = 50  # Hue Entertainment's documented max packet rate
# (matches the 50 Hz analysis frame rate, so every analysed hop becomes a frame)
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

# ffmpeg is only ever pointed at http(s) URLs (MA stream URLs, artwork,
# Subsonic endpoints) — every source absolutises relative paths first. Locking
# the protocol set down stops a malicious URL (a compromised media server, a
# crafted entity_picture) from steering ffmpeg into file://, concat: and
# friends (local-file read / SSRF surface). Passed as an input option, so the
# pipe:1 PCM output is unaffected.
FFMPEG_PROTOCOL_ARGS: Final = ("-protocol_whitelist", "http,https,tcp,tls,crypto")

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

# LedFx-style melbank: a finer, perceptually-spaced power spectrum (per-bin gain
# normalised + exponentially smoothed) that drives the engine's *continuous*,
# always-alive reactive brightness — the room moves with the music whether or
# not a beat is detected. 16 log-spaced bins from ~40 Hz to 11 kHz is plenty for
# a handful of lamps and stays cheap (reuses the FFT already taken per hop).
MELBANK_BINS: Final = 16
MELBANK_FMIN: Final = 40.0
MELBANK_FMAX: Final = 11000.0


DEFAULT_RESTORE_LIGHTS: Final = False  # opt-in: restore exact pre-sync light state
DEFAULT_BRIGHTNESS: Final = 1.0  # master brightness ceiling (0..1)
DEFAULT_TIMING_MS: Final = 0  # +ve delays lights, -ve advances (within buffer)
TIMING_BUFFER_MS: Final = 200  # baseline delay buffer enabling -ve offsets
# Estimated latency of the light pipeline itself, from the moment we emit a
# frame to photons changing in the room: the bridge only relays to the bulbs
# over Zigbee at ~25 Hz (so up to ~40 ms there) plus the bulb's own ramp. This
# is the single latency we pre-empt: when a source's analysis leads the audible
# sound (snapcast) we delay frames by the lead *minus* this, and scheduled
# playback generates frames this far ahead, so photons land on the beat. One
# documented knob to tune on hardware.
BULB_LATENCY_MS: Final = 100
LIGHT_PIPELINE_MS: Final = BULB_LATENCY_MS  # backwards-compatible alias


class ColorScheme(StrEnum):
    """Selectable colour themes — smooth, harmonious palettes plus album art."""

    ALBUM_ART = "album_art"
    SONG = "song"  # colours derived from the song's own harmony (key/pitch -> hue)
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

    AUTO = "auto"  # pick Subtle/Medium/High from the song's tempo (see AUTO_BPM_*)
    SUBTLE = "subtle"  # seamless: steady level, colour just flows/shifts smoothly
    MEDIUM = "medium"  # gentle club: visible dimming, soft flashes on strong beats
    HIGH = "high"  # the band: per-instrument spatial split, kicks/guitar/vocals
    INTENSE = "intense"  # club: whole room follows energy + bursts on every beat, colour jumps
    EXTREME = "extreme"  # max club: whole room dark<->full-bright with energy, fireworks every beat


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

# Auto intensity: map the song's locked BPM to Subtle/Medium/High. Ballads sit
# below AUTO_BPM_LOW, up-tempo tracks above AUTO_BPM_HIGH, everything between is
# Medium. AUTO_BPM_MARGIN is a hysteresis dead-zone half-width so a track sitting
# on a boundary doesn't oscillate between two levels. Intense/Extreme are never
# chosen automatically - they stay manual-only.
AUTO_BPM_LOW: Final = 95.0
AUTO_BPM_HIGH: Final = 125.0
AUTO_BPM_MARGIN: Final = 6.0

PLATFORMS: Final = ["switch", "select", "number", "button", "sensor"]

# Dispatcher signal fired whenever the library pre-warm status changes.
SIGNAL_PREWARM: Final = f"{DOMAIN}_prewarm_update"


def signal_area_update(area_id: str) -> str:
    """Dispatcher signal fired when an area's sync state changes."""
    return f"{DOMAIN}_area_update_{area_id}"
