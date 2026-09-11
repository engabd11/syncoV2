# Hue Synco v1.56.1 — Initial HACS Release

Music-reactive Philips Hue Entertainment lighting for Home Assistant, driven by Music Assistant players. Beat-accurate light shows with a bundled dashboard card.

## Features

- **Beat-accurate light sync** — Real-time Philips Hue Entertainment API (UDP streaming) driven by Music Assistant playback
- **Auto intensity** — Automatically picks the right intensity level based on the song's vibe, not its peak
- **Auto timing** — Drives lights from a real playback clock for reliable beat sync
- **Five intensity presets** — Subtle, Medium, High, Intense, and Extreme
- **Multiple effects** — Pulse, Wave-peek, Fireworks, Movies, and more
- **Album-art colour palettes** — Lights match the album artwork hues
- **Ambient idle glow** — Slow wandering glow when paused or empty
- **Bundled dashboard card** — Custom Lovelace card with tablet and mobile layouts
- **Music Assistant library browsing** — Browse, search, and play from the card
- **Speaker grouping** — Group/ungroup MA players from the card
- **Library management** — Reanalyse and delete-cache buttons, cache size/count sensors
- **Advanced live tunables** — Advanced switch with per-knob controls under the intensity
- **Hue Entertainment API compliance** — Honours external stops, per-light gamut mapping, REST pacing

## Requirements

- Home Assistant 2024.12.0 or later
- Philips Hue bridge with Entertainment API support
- Music Assistant integration installed and configured
- `numpy` (auto-installed by HACS)

## Installation

1. Install via HACS (Home Assistant Community Store)
2. Add `hue_music_sync` integration via Settings → Devices & Services
3. Select your Hue bridge and configure your entertainment areas

## Configuration

Configuration is done entirely via the UI (config flow). No YAML required.