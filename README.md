# Hue Synco for Home Assistant

A custom Home Assistant integration that drives **Philips Hue Entertainment areas** in real-time with music from **Music Assistant**. Beat detection, frequency analysis, and spatial choreography stream directly to the bridge over the Hue Entertainment API (~40 Hz, DTLS-encrypted), while a bundled dashboard card visualises everything live.

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
- **Salience-proportional reactions** — every flash, wave, and scheduled pulse scales with the sound's *absolute* loudness relative to the track: a quiet pluck gives a small dim pulse, the drop slams full. Tiny sounds can never flash at full brightness, and a locked beat grid stops slamming through breakdowns
- **Vocal rejection** — onsets are classified by how broadband their spectral flux is: drums splash across the spectrum, sung vowels and sustained tones stay narrow. Narrowband onsets are muted (with a soft knee, per intensity mode) so the lights follow the instruments, not the singing
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
- **Instrument role assignment** — lights are divided into bass, guitar, and vocal roles, spread evenly around the room and rotated every few bars; the split scales cleanly from 1 to 10 lights so larger rooms stay balanced
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
- **Play-the-beats drum pad** — a Low / Mid / High pad page where each pad drives a third of the room; while it's open the automatic beats pause so *your taps* drive the lights, with the music's colour and energy still flowing underneath
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

Plus, once per installation:

| Entity | Type | Description |
|---|---|---|
| Analyse library | Button | Kicks off the background library pre-analysis (same as the `prewarm_library` service) |
| Library analysis | Sensor | Live progress of the pre-analysis, with failure details in its attributes |

### Modes

Every mode also sets how *picky* the beat selection is: Subtle reacts only to loud, unambiguous percussion, High is strictly proportional with a firm vocal guard, and Extreme lets nearly everything through (still proportional to loudness). There is no separate sensitivity slider — the mode is the sensitivity.

| Mode | Flash limiter | Character |
|---|---|---|
| **Subtle** | On | Gentle spatial gradient, soft color drift, small beat steps |
| **Medium** | On | Visible dimming, soft flashes on stronger beats, wide color spread |
| **High** | On | Per-instrument spatial split (bass / guitar / vocal) spread around the room, **dynamically assigned to the instruments actually playing** so no lamp sits dull on an absent one, re-dealt as the song changes |
| **Intense** | Off | Unified club with a fast but **smooth** dim↔bright swing on the beat (no strobe), colour shifting each hit; keeps a soft glow in the gaps (higher dark floor than Extreme) |
| **Extreme** | Off | Same quick smooth swing, but a **true dark room** (floor 0) — the quiet parts go black and every beat brightens the whole room out of the dark; widest range, fastest response |

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
| `hue_music_sync.prewarm_library` | Analyse your whole Music Assistant library in the background and cache it to disk, so **every** track plays instantly with full beat-accurate reaction the first time too — not just on a repeat or in a queue |

**Pre-analysing the library** (`prewarm_library`) is the way to make a brand-new
single track react immediately. It runs gently in the background — one track at
a time, yielding to live playback — and is resumable, so re-running only
analyses what's new (run it once after setup, or schedule it from an
automation). For a Navidrome / OpenSubsonic library, set the library URL and
login in the options first so stream URLs can be built without a playback
session.

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
Real-time analysis (5-band FFT, 16-bin melbank, SuperFlux onsets, tempo,
absolute-loudness salience + onset broadbandness for event selection)
        ↓
Offline track map (beat grid, downbeats, section boundaries — analysed once,
then cached to disk so the same track plays instantly the next time)
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
- **Track analysis takes a moment** — full offline analysis can take 10+ seconds on slower hardware; the fallback runs during this window, then the show upgrades to the real analysis as soon as it is ready (transient failures, e.g. a busy library, are retried automatically, and the next track is pre-analysed for gapless transitions). Run the `prewarm_library` service once to analyse the whole library ahead of time and remove this first-play delay entirely
- **Playback position granularity** — players that report position coarsely (e.g. Sonos at ~500 ms) reduce track-map timing precision
- **Cache re-analysis after upgrades** — when an update changes the track-map format, previously analysed tracks re-analyse once in the background on their next play (or in one sweep via `prewarm_library`)
- **Intense / Extreme strobing** — these modes bypass the flash limiter by design; they are not suitable for anyone with photosensitivity
