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
# The hue-application-id (fetched from /auth/v1) used as the DTLS PSK identity.
# Per the Hue Entertainment API spec, this is the correct PSK identity — not the
# app key. Stored so it doesn't need re-fetching every session.
CONF_APP_ID: Final = "application_id"
CONF_AREAS: Final = "areas"  # list of enabled entertainment_configuration ids
# The bridge's self-signed TLS certificate (PEM), captured at pairing time
# (trust-on-first-use) so every later CLIP call verifies it is talking to the
# same bridge instead of accepting any certificate.
CONF_BRIDGE_CERT: Final = "bridge_certificate"

# --- Per-area option keys ------------------------------------------------
CONF_MODE: Final = "mode"
CONF_AUTO_LEVELS: Final = "auto_levels"
CONF_EFFECT: Final = "effect"
CONF_COLOUR: Final = "colour"
CONF_BRIGHTNESS: Final = "brightness"
CONF_MEDIA_PLAYER: Final = "media_player"
CONF_LATENCY_MS: Final = "latency_ms"
CONF_TIMING_MS: Final = "timing_ms"
CONF_AUTO_TIMING: Final = "auto_timing"
# Advanced live tunables: opt-in per-area knobs that scale the active mode's
# render params during playback (the card reveals them under the intensity).
CONF_ADVANCED: Final = "advanced"  # show + apply the advanced tunables
CONF_TUNABLES: Final = "tunables"  # dict {name: factor}; 1.0 = the mode's coded value
CONF_SNAPSERVER_HOST: Final = "snapserver_host"
# Where Music Assistant serves the Sendspin protocol. Normally derived from MA's
# own base URL (same host, own port), so this is only an escape hatch for setups
# where that lookup fails. Sendspin carries a synchronised microsecond clock and
# timestamped progress anchors, which is a far better light-timing reference
# than the player's coarse media_position -- see audio/sendspin.py.
CONF_SENDSPIN_HOST: Final = "sendspin_host"
# OpenSubsonic / Navidrome library (optional): lets us fetch & analyse library
# tracks directly when Music Assistant won't expose a tappable stream URL
# (e.g. Sendspin playing an OpenSubsonic track).
CONF_SUBSONIC_URL: Final = "subsonic_url"
CONF_SUBSONIC_USER: Final = "subsonic_user"
CONF_SUBSONIC_PASSWORD: Final = "subsonic_password"
CONF_RESTORE_LIGHTS: Final = "restore_lights"  # snapshot + restore light state on stop

# Active library/playback backend for the Synco media player. Exactly one is
# active at a time, and it can be switched at runtime — e.g. fall back to a
# direct Navidrome/OpenSubsonic connection when Music Assistant is unavailable.
# Stored in the config-entry options.
CONF_ACTIVE_BACKEND: Final = "active_backend"
BACKEND_MA: Final = "music_assistant"  # browse/play through Music Assistant
BACKEND_SUBSONIC: Final = "subsonic"  # browse/play a Navidrome/OpenSubsonic server directly
DEFAULT_BACKEND: Final = BACKEND_MA

# --- Defaults ------------------------------------------------------------
# How far ahead of the audible position the Music Assistant tap decodes. It is
# also the lead the tap *reports*, so the delay buffer holds frames for
# (latency - LIGHT_PIPELINE_MS) and the photons land on the beat. Set equal to
# TIMING_BUFFER_MS + LIGHT_PIPELINE_MS (see below) so the applied baseline works
# out to TIMING_BUFFER_MS: the live tap then has exactly the same symmetric
# +-200 ms of trim headroom that scheduled track-map playback has.
DEFAULT_LATENCY_MS: Final = 300
# The previous default. latency_ms has never been settable from the UI, a
# service or an entity, so a stored value equal to this is the old default
# rather than a deliberate choice, and is migrated (coordinator.AreaSettings).
LEGACY_DEFAULT_LATENCY_MS: Final = 150
DEFAULT_INTENSITY: Final = 1.0
DEFAULT_STREAM_FPS: Final = 60  # Hue Entertainment API recommends 50-60 Hz streaming
# The bridge relays to bulbs at max 25 Hz over Zigbee, so the visible effect rate
# is capped regardless; 60 Hz gives smoother temporal resolution for the continuous
# melbank/spatial wave layer. The analysis frame rate stays at ~50 Hz (ANALYSIS_HOP/
# ANALYSIS_SAMPLE_RATE), so some frames are re-sent — the keepalive loop handles
# that naturally.
DEFAULT_NAME: Final = "hue_music_sync#ha"

# Hue entertainment streaming
HUE_DTLS_PORT: Final = 2100
HUE_STREAM_PROTOCOL: Final = b"HueStream"
HUE_STREAM_VERSION: Final = b"\x02\x00"
KEEPALIVE_INTERVAL: Final = 9.0  # bridge drops the channel after ~10s of silence
# The Entertainment API spec allows up to 20 channels per UDP streaming message;
# the bridge handles splitting these into its internal ~10-light Zigbee batches.
# Larger areas (multiple lamps + gradient-strip segments) are still split across
# packets when they exceed this limit.
MAX_CHANNELS_PER_PACKET: Final = 20

# Hue REST limits (Hue System Performance). The bridge translates each CLIP
# command into one Zigbee message per parameter and can only schedule ~25 of
# those per second in total; the published guidance is ~10 commands/s to /light
# with a 100 ms gap. Exceeding it buffers silently inside the bridge (seconds of
# latency) and eventually drops commands with a type-901 error. Only the restore
# path writes lights at all — the show itself goes over Entertainment streaming.
LIGHT_COMMAND_MIN_INTERVAL: Final = 0.1

# Server-Sent Events. Core Concepts: "It is important to not try to stay up to
# date by performing repeated GET requests" — subscribe to /eventstream instead.
# The bridge coalesces changes into at most one container per second.
EVENTSTREAM_PATH: Final = "/eventstream/clip/v2"
EVENTSTREAM_RECONNECT_BASE_S: Final = 2.0
EVENTSTREAM_RECONNECT_MAX_S: Final = 60.0

# Hue cloud discovery (discovery.meethue.com). Rate limited by Signify to one
# request per 15 minutes per client, so results are cached for longer than that.
DATA_DISCOVERY_CACHE: Final = "_discovery_cache"
DISCOVERY_CACHE_TTL: Final = 15 * 60

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
# Auto timing: when on, the lights are kept locked to the player automatically —
# through seeks, skips and playback stutters — and on a live tap the analyser's
# own startup slippage is measured and corrected per song. It is applied *on top
# of* the manual timing offset, never in place of it: the room's acoustic delay
# (speakers, bulb ramp, taste) has no software reference to be discovered from,
# so that stays the user's. Opt-in; manual is unchanged when off.
DEFAULT_AUTO_TIMING: Final = False
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
    # Album colours v2: the same faithful swatch extraction, but each colour
    # carries its share of the cover (population weight) and the lights spend
    # time on it proportionally — a 90% green / 10% red cover renders a green
    # room that shifts to red in moments, not a 50/50 gradient.
    ALBUM_ART_V2 = "album_art_v2"
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
    # Extra curated palettes (fill the picker with distinct, sensible themes)
    NEON = "neon"  # electric cyan / magenta / green / hot pink
    PEACOCK = "peacock"  # teal / blue / emerald / gold jewel tones
    CITRUS = "citrus"  # lemon / lime / orange / coral
    ROSEGOLD = "rosegold"  # blush / rose / copper / champagne


class SyncMode(StrEnum):
    """Samsung-style intensity ladder controlling behaviour only.

    Sets how reactive the lights are (dimming range, beat brightening, shimmer)
    *relative to* the separate master brightness — not the absolute level.
    Parameters per mode live in ``effects.modes.MODE_PARAMS``.
    """

    AUTO = "auto"  # pick a rung live from the music's intensity, gated to an enabled set
    SUBTLE = "subtle"  # seamless: steady level, colour just flows/shifts smoothly
    MEDIUM = "medium"  # gentle club: visible dimming, soft flashes on strong beats
    HIGH = "high"  # the band: per-instrument spatial split, kicks/guitar/vocals
    INTENSE = "intense"  # club: whole room follows energy + bursts on every beat, colour jumps
    EXTREME = "extreme"  # max club: dark room, instruments split across lamps, fast beats chase side-to-side


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

# Legacy BPM→Subtle/Medium/High mapping (``effects.modes.auto_mode_for_bpm``),
# superseded by the musical ``AutoIntensityPicker`` but kept for reference/tests.
# AUTO_BPM_MARGIN is a hysteresis dead-zone half-width so a track sitting on a
# boundary doesn't oscillate between two levels.
AUTO_BPM_LOW: Final = 95.0
AUTO_BPM_HIGH: Final = 125.0
AUTO_BPM_MARGIN: Final = 6.0

# The intensity ladder in ascending order (Auto is the picker itself, not a
# rung). Roughly what each rung is *for*:
#
#   Subtle   very soft music — lofi, ambient. Most songs never qualify.
#   Medium   Subtle with a bit more movement.
#   High     lights dance, brightness fairly steady. Soft/dance, mid vibes.
#   Intense  lots of beats, fast melody — common on energetic tracks.
#   Extreme  the highest peaks only, where Intense doesn't cut it.
#
# The Auto picker honours that by scoring how hard a song goes on an ABSOLUTE
# scale (``effects.modes.song_character``, measured offline by
# ``trackmap.build_intensity_profile``) and letting that decide the band of the
# ladder the song may use; its own section arc then moves within that band. So a
# chill track tops out low however loud its own chorus is, and only a genuinely
# heavy one reaches Extreme. The enabled set is a palette rather than a forced
# range: the ladder is rescaled onto whatever you selected, so a banger reaches
# the top of your selection and a chill track doesn't — and no song has to use
# every rung. Making High the lowest enabled rung still makes it the floor.
INTENSITY_LADDER: Final = (
    SyncMode.SUBTLE,
    SyncMode.MEDIUM,
    SyncMode.HIGH,
    SyncMode.INTENSE,
    SyncMode.EXTREME,
)
# Which rungs Auto may choose from until the user says otherwise. Subtle /
# Medium / High keeps the historical behaviour (Intense / Extreme stay opt-in),
# so an existing Auto user sees no change until they add a rung.
DEFAULT_AUTO_LEVELS: Final = (SyncMode.SUBTLE, SyncMode.MEDIUM, SyncMode.HIGH)

# Advanced live tunables: each is a multiplier on the active mode's relevant
# params (1.0 = the mode's coded value), applied live when Advanced is on. They
# no-op gracefully on a mode that doesn't use a given param. The card shows them
# as 0-200% sliders under the intensity picker.
TUNABLE_KEYS: Final = (
    "reactivity",    # flash punch: spectral_pop, mel_flux_gain, beat_gain
    "glow",          # continuous room brightness: melbank_gain
    "movement",      # spatial motion: rotate_rate, rotate_swing, wave_speed
    "contrast",      # small-vs-big peak spread: flash_gamma
    "colour_speed",  # colour drift speed: colour_speed, colour_flow
    "loudness",      # per-band absolute-loudness follow: band_loud_strength
)
TUNABLE_MIN: Final = 0.0
TUNABLE_MAX: Final = 2.0
DEFAULT_TUNABLE: Final = 1.0


def sanitize_tunables(data) -> dict[str, float]:
    """Keep only known tunables, coerced to float and clamped to the valid range."""
    out: dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    for key in TUNABLE_KEYS:
        if key in data:
            try:
                v = float(data[key])
            except (TypeError, ValueError):
                continue
            out[key] = max(TUNABLE_MIN, min(TUNABLE_MAX, v))
    return out


PLATFORMS: Final = ["switch", "select", "number", "button", "sensor"]

# Dispatcher signal fired whenever the library pre-warm status changes.
SIGNAL_PREWARM: Final = f"{DOMAIN}_prewarm_update"


def signal_area_update(area_id: str) -> str:
    """Dispatcher signal fired when an area's sync state changes."""
    return f"{DOMAIN}_area_update_{area_id}"
