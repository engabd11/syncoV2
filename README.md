# Hue Synco for Home Assistant

A custom Home Assistant integration that drives **Philips Hue Entertainment areas** in real-time with music from **Music Assistant** — no microphone, no cloud relay. Beat detection, frequency analysis, and spatial choreography stream directly to the bridge over the Hue Entertainment API (~40 Hz, DTLS-encrypted), while a bundled dashboard card visualises everything live.

<p align="center">
  <img src="docs/card.png" alt="Hue Synco dashboard card" width="360" />
</p>

---

## What it does

Hue Synco listens to whatever is playing through Music Assistant and translates the audio into a synchronized light show across your Hue entertainment area.

Every track is analysed once in the background — beats are located precisely, downbeats and section boundaries are found (verse, chorus, build, drop). During playback those events are **scheduled** ahead of time, so the choreography reacts exactly on the beat rather than chasing it. A continuous spectral layer (a 16-band melbank spread spatially across the room) keeps every lamp alive between beats.

For Snapcast-backed players the live audio stream is tapped directly; for streaming-source players (Squeezelite, Slimproto) the stream URL is decoded in sync with the reported playback position; and for players without a tappable stream (AirPlay, Chromecast, Sonos, DLNA, ESPHome) the pre-analysed track map drives the show at full beat accuracy.

---

## Features

### Audio analysis
- **Full-track beat tracking** — dynamic-programming beat tracker run offline so beats are pre-located, not guessed in real time
- **SuperFlux onset detection** — spectral flux on log-compressed magnitudes with vibrato immunity; separate bass/kick, mid/guitar, and broadband streams avoid hi-hat false positives
- **5-band frequency decomposition** with per-band automatic gain control (normalises loud and quiet tracks to the same 0–1 range)
- **16-bin melbank** — continuous exponentially-smoothed spectrum spread left-to-right across the room so the show is always alive
- **Song structure detection** — builds, drops, verses, and choruses are identified; brightness swells on drops, desaturates during builds, breathes during breakdowns

### Player support
| Player type | Audio source |
|---|---|
| Snapcast (Music Assistant) | Real-time stream tap with automatic buffer-alignment |
| Squeezelite / Slimproto | Position-locked stream decoding (re-syncs on drift) |
| AirPlay, Chromecast, Sonos, DLNA, ESPHome, groups | Pre-analysed track map (full beat accuracy, no live stream required) |
| Any player | Metadata fallback — gentle LFO animation when no stream is available |

### Choreography
- **5 intensity modes** (Subtle → Medium → High → Intense → Extreme) sharing the same unified renderer with different parameters
- **Instrument role assignment** — lights are divided into bass, guitar, and vocal roles that rotate every few bars to keep the show surprising
- **3D spatial waves** — beat wavefronts sweep the room using actual lamp positions from the entertainment area; low frequencies to one side, highs to the other; treble assigned to higher lamps
- **Beat highlight selection** — brightness pops only on beats that stand out against the recent 24-beat window, so not every beat looks the same

### Color
- **Album art extraction** — dominant colors pulled from cover art in perceptual CIELAB space; mood tones preserved (muted artwork stays muted)
- **Song harmony coloring** — derives a palette from the track's pitch content; each section's dominant pitch classes map to hues across the spectrum
- **11 preset themes** — Sunset, Ocean, Forest, Lavender, Ember, Aurora, Rainbow, Tropical, Savanna, Blossom, Honolulu, Galaxy

### Eye safety (flash-limited modes)
Subtle, Medium, High, and Movies apply a WCAG 2.3.1-compliant flash limiter: hard cap of 3 whole-room flashes per second, a minimum brightness floor that prevents pure-black strobing, a red saturation guard, and per-frame xy color slew limits for smooth transitions.

Intense and Extreme deliberately bypass the flash limiter for maximum impact — those modes are unsuitable for photosensitive individuals.

### Dashboard card
A custom Lovelace card is registered automatically — no manual resource download needed. It provides:
- Real-time band-energy visualiser bars at ~20 Hz driven by the actual analysis output
- Room mirror showing every lamp at its real position, glowing in the exact color being streamed, with instrument-role rings
- Song-structure timeline with energy silhouette and a playhead; the next section pulses as a drop approaches
- Transport controls (previous / play-pause / next) that drive the media player directly
- Tap-to-sync calibrator: tap along for 8 beats, the card measures the offset and writes it to the timing slider automatically
- Per-mode intensity previews with marquee titles for long track names
- Respects `prefers-reduced-motion` and pauses animation when the card is off-screen

---

## Requirements

- **Home Assistant** (any recent version with HACS support)
- **Music Assistant** integration installed and connected
- **Philips Hue Bridge v2** (the square one — v1 does not support Entertainment streaming)
- **Entertainment area** created and configured in the Hue app (Hue Synco drives existing areas, it does not create them)
- **ffmpeg** — included with HAOS, Container, and Supervised installs

Optional:
- **Snapcast server** — for real-time buffer-aligned audio on Snapcast-backed players
- **OpenSubsonic / Navidrome** — for direct library-track analysis on players where MA does not expose a stream URL

---

## Installation

### HACS (recommended)
1. In HACS, go to **Integrations → Custom repositories**
2. Add this repository URL and select **Integration** as the category
3. Install **Hue Synco** and restart Home Assistant

### Manual
1. Copy the `custom_components/hue_music_sync` folder into your `config/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **Hue Synco**
2. Enter your Hue bridge IP address (or let it be discovered)
3. Press the **link button** on the bridge when prompted
4. Select which entertainment areas to enable
5. The integration creates a switch, mode selector, effect selector, colour selector, brightness slider, and timing slider for each area

> Create and arrange your entertainment areas in the **Hue app** before setting up Hue Synco — the integration discovers whatever areas already exist on the bridge.

---

## Controls

Each entertainment area gets the following entities:

| Entity | Type | Description |
|---|---|---|
| Sync | Switch | Starts and stops the light show for this area |
| Mode | Select | Choreography intensity (see below) |
| Effect | Select | Rendering style (Music, Movies, Fireworks) |
| Colour | Select | Color palette source |
| Brightness | Number | Master brightness ceiling (5–100%) |
| Timing offset | Number | Manual sync trim in milliseconds (-500 to +500) |

### Modes

| Mode | Flash limiter | Character |
|---|---|---|
| **Subtle** | On | Gentle spatial gradient, soft color drift, small beat steps |
| **Medium** | On | Visible dimming, soft flashes on stronger beats, wide color spread |
| **High** | On | Per-instrument spatial split; roles rotate every few bars |
| **Intense** | Off | Whole-room unified reaction, hard flashes on standout beats |
| **Extreme** | Off | Maximum range, every beat pulses, fastest response |

### Effects

- **Music** — full beat/frequency choreography (default)
- **Movies** — calm, non-distracting; brightness follows soundtrack energy with warm cinematic drift, no flashing
- **Fireworks** — bursts ignite on big beats with a rapid fade-out

### Colour schemes

- **Album Art** — extracted from the current track's cover art
- **Song** — derived from the track's harmonic content
- **Preset themes** — Sunset, Ocean, Forest, Lavender, Ember, Aurora, Rainbow, Tropical, Savanna, Blossom, Honolulu, Galaxy

---

## Services

| Service | Description |
|---|---|
| `hue_music_sync.activate` | Start sync; optionally set mode, effect, colour, brightness, and media player |
| `hue_music_sync.deactivate` | Stop sync |
| `hue_music_sync.set_options` | Update any setting live without restarting the sync session |

---

## Options

Access via **Settings → Devices & Services → Hue Synco → Configure**:

| Option | Description |
|---|---|
| Snapcast server host | Address of your Snapcast server for real-time audio tap |
| Restore lights on stop | Snapshot and restore light state when sync stops |
| OpenSubsonic URL / credentials | For direct library-track analysis via a Navidrome or compatible server |

---

## How the audio pipeline works

```
Audio source (Snapcast tap / stream URL / track map)
        ↓
Real-time analysis (5-band FFT, 16-bin melbank, SuperFlux onsets, tempo)
        ↓
Offline track map (beat grid, downbeats, section boundaries — analysed once, cached)
        ↓
Album art → CIELAB color palette extraction
        ↓
Effect engine (instrument roles, spatial waves, brightness envelopes, palette sampling)
        ↓
Eye safety stage (flash limiter, brightness floor, gamut clamp, color slew)
        ↓
HueStream encoder (RGB → xy chromaticity + brightness, Gamut C clamping)
        ↓
Pure-Python DTLS 1.2 (PSK auth, AES-128-GCM) → Hue Bridge (~40 Hz)
```

The DTLS transport is implemented in pure Python — no external OpenSSL dependency — and covers exactly what the bridge needs: PSK handshake and AES-128-GCM record encryption with 9-second keepalives.

---

## Known limitations

- **Hue Bridge v2 only** — the v1 (round) bridge does not support Entertainment streaming
- **One area streaming at a time per bridge** — a single DTLS channel per bridge; multiple bridges each get their own entry
- **Track analysis takes a moment** — full offline analysis can take 10+ seconds on slower hardware; the fallback metadata animation runs during this window
- **Playback position granularity** — players that report position coarsely (e.g. Sonos at ~500 ms) reduce track-map timing precision
- **Intense / Extreme strobing** — these modes bypass the flash limiter by design; they are not suitable for anyone with photosensitivity
