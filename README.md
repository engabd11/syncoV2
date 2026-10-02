# Hue Synco for Home Assistant

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Licence: MIT](https://img.shields.io/badge/Licence-MIT-yellow.svg)](https://github.com/engabd11/syncoV2/blob/main/LICENSE)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.12%2B-blue)
[![Latest release](https://img.shields.io/github/v/release/engabd11/syncoV2?label=release)](https://github.com/engabd11/syncoV2/releases/latest)

**Beat-mapped Philips Hue lighting for Home Assistant.**

Hue Synco follows any media player in Home Assistant, Music Assistant players first (Sendspin included), and lights a Hue entertainment area on the beat. Each track is analysed once, so the lights are scheduled ahead of the music and land exactly on the beat, and a dashboard card that comes with the integration mirrors the whole show live.

Free and open source (MIT), built by [Cyborg Automation AU](https://cyborgautomation.com.au/pages/hue-synco). Home Assistant lists it as **Hue Synco** (domain `hue_music_sync`); the repository is named syncoV2.

<p align="center">
  <img src="https://raw.githubusercontent.com/engabd11/syncoV2/main/docs/card-tablet.png" alt="Hue Synco tablet dashboard card" width="760" />
</p>
<p align="center">
  <img src="https://raw.githubusercontent.com/engabd11/syncoV2/main/docs/card.png" alt="Hue Synco mobile dashboard card" width="320" />
</p>

## What you get

- **Lights on the beat.** Beats, downbeats and sections are found once per track and cached. During playback they are scheduled ahead of time, and a continuous 16-band spectrum layer keeps every lamp alive between beats.
- **Reactions in proportion.** Brightness scales with the real loudness of each sound. Drums and instruments drive the beats while sung vowels are filtered out, so the lights follow the music rather than the singing.
- **Works with the players you have.** Any Home Assistant media player, Music Assistant first: Sendspin, Squeezelite, AirPlay, Chromecast, Sonos, DLNA, ESPHome and groups. Navidrome and OpenSubsonic libraries are supported directly.
- **Your look, your way.** Six intensity modes (Auto picks one for each song), three effects (Music, Movies, Fireworks) and 19 colour options: album colours, song colours and 16 themes.
- **A dashboard card for phones and wall tablets.** Room mirror, now playing, song timeline, controls, library browsing, speaker grouping and Beat Pads in one card that registers itself.
- **Movie mode.** Hand the same entertainment area to [Hue Ghost](https://github.com/engabd11/HueGhost) for films, TV and games.
- **Made for automations.** Each area has entities for sync, intensity, effect, colour, brightness and timing, and six services cover the rest.
- **Straight to the bridge.** Frames stream to the Hue Bridge at 60 a second over the Entertainment API (DTLS 1.2). The bridge relays to the lamps at 25 Hz or less.

## Works with

Any Music Assistant player works out of the box. Hue Synco picks the best audio source for each player and upgrades live when a better one becomes available.

| Player type | How it is followed |
|---|---|
| **Sendspin** | Live audio from the track's stream, with an exact playhead from Sendspin's synchronised clock |
| **Squeezelite / Slimproto** | Live audio from the track's stream, re-synced on drift |
| **AirPlay, Chromecast, Sonos, DLNA, ESPHome and groups** | A pre-analysed track map for full beat accuracy |
| **Any other media player** | A gentle animation from metadata, upgraded to a real source as soon as one is available |

Point Hue Synco at a **Navidrome or OpenSubsonic** server to analyse your library directly. More in [Players and libraries](https://github.com/engabd11/syncoV2/blob/main/docs/players.md).

## Requirements

- **Home Assistant** 2024.12 or newer
- **Music Assistant** (recommended). Any other Home Assistant media player works too, with reduced accuracy.
- **Philips Hue Bridge v2** (the square one). The round v1 bridge predates Entertainment streaming.
- An **entertainment area** created and arranged in the Hue app. Hue Synco lights the areas you have already made, and the lamp positions you set there drive the spatial choreography and the card's room mirror.
- **ffmpeg**, which is included with Home Assistant OS, Container and Supervised installs.

## Install

### HACS (custom repository)

1. In HACS, open **⋮ → Custom repositories**
2. Add `https://github.com/engabd11/syncoV2` with type **Integration**
3. Install **Hue Synco** and restart Home Assistant

### Manual

1. Copy the `custom_components/hue_music_sync` folder into your `config/custom_components/` directory
2. Restart Home Assistant

## Set up

1. Go to **Settings → Devices & Services**. Bridges on your network are discovered over mDNS and appear ready to set up. If yours is missing, choose **Add Integration → Hue Synco** and enter its IP (the field is pre-filled when Hue's discovery service can find the bridge).
2. Press the **link button** on the bridge when prompted. Hue Synco checks the bridge's certificate before pairing (see [Security](https://github.com/engabd11/syncoV2/blob/main/docs/security.md)).
3. Select which entertainment areas to enable.
4. Add the **Hue Synco Card** to a dashboard. It is already in the card picker and fills in a working template.

## Movie mode (Hue Ghost)

Music sync covers music. For films, TV and games, the same entertainment area can follow the screen instead, with a PC standing in for a Hue Sync Box. [Hue Ghost](https://github.com/engabd11/HueGhost) is a free Windows app that plays a silent copy of whatever your TV's Jellyfin client is playing in step with the TV, and the official Hue Sync desktop app streams that copy to the area. It also lights up for apps and games on the PC, and for Jellyfin music.

Hue Synco switches Hue Ghost on and off from Home Assistant and hands the area over cleanly, because a bridge streams to one app per entertainment area at a time. Enter the Hue Ghost PC host, port and token under **Configure** and a **Hue Ghost** device appears, with a Global sync light, status sensors and a switch for each source. Hue Ghost 2.4.0 or newer gives the full set of controls.

Entities, versions and the bundled Hue Ghost card are in [Movie mode](https://github.com/engabd11/syncoV2/blob/main/docs/movie-mode.md).

## ⚠️ Photosensitivity warning

Audio-reactive lighting can produce rapid whole-room brightness swings. Most modes pass through an eye-safety limiter, at different levels:

- **Strict** (built around WCAG 2.3.1): Subtle, Medium, High and the Movies effect. A hard cap of 3 whole-room flashes per second, a minimum brightness floor that prevents pure-black strobing, a saturated-red guard and per-frame colour slew limits.
- **Relaxed**: Intense. A much higher flash budget (8 whole-room flashes per second) that real music never reaches, so the club character is untouched, and it still hard-caps genuine strobe output.
- **Bypassed**: Extreme. The limiter is off entirely for the sharpest, fastest response. Its per-lamp spatial separation means the whole room rarely flashes as one, but there is no cap.

Auto follows the rung it picks. By default Auto chooses from Subtle, Medium and High, so it runs on the strict limiter unless you add Intense or Extreme to its range.

**Relaxed and Bypassed do not meet WCAG 2.3.1. Intense and Extreme are not suitable for anyone with photosensitivity.** If anyone in the room may be photosensitive, stay on Auto, Subtle, Medium or High.

## Documentation

- [Features](https://github.com/engabd11/syncoV2/blob/main/docs/features.md): audio analysis, choreography, colour and effects
- [Players and libraries](https://github.com/engabd11/syncoV2/blob/main/docs/players.md): how each player type is followed, Navidrome and OpenSubsonic, legacy sources
- [Dashboard card](https://github.com/engabd11/syncoV2/blob/main/docs/dashboard-card.md): the phone and tablet cards and their configuration
- [Controls](https://github.com/engabd11/syncoV2/blob/main/docs/controls.md): entities, Advanced controls, timing and the intensity modes
- [Movie mode](https://github.com/engabd11/syncoV2/blob/main/docs/movie-mode.md): the Hue Ghost device, its card and upgrade notes
- [Services and options](https://github.com/engabd11/syncoV2/blob/main/docs/services-and-options.md): services, library analysis and the integration options
- [How it works](https://github.com/engabd11/syncoV2/blob/main/docs/how-it-works.md): the pipeline from audio to bridge
- [Security](https://github.com/engabd11/syncoV2/blob/main/docs/security.md): certificates, pairing, ffmpeg and logging
- [Troubleshooting](https://github.com/engabd11/syncoV2/blob/main/docs/troubleshooting.md): fixes and known limits
- [Development](https://github.com/engabd11/syncoV2/blob/main/docs/development.md): code layout and tests

Questions and bug reports are welcome in [Issues](https://github.com/engabd11/syncoV2/issues).

## Part of the Cyborg Automation lighting projects

- **[CAMusic](https://github.com/engabd11/CAMusic)** is an Android music player made for Sendspin players on Music Assistant, with Hue light shows.
- **Hue Synco** lights Hue to the music of any media player in Home Assistant, and controls Hue Ghost from its Movie mode.
- **[Hue Ghost](https://github.com/engabd11/HueGhost)** lights films, games and apps from your PC.

Use one or all three. They can share an entertainment area by taking turns, because an area streams from one app at a time. See all three on the [open source lighting page](https://cyborgautomation.com.au/pages/oss-lighting).

## Licence and credits

MIT licensed. See [LICENSE](https://github.com/engabd11/syncoV2/blob/main/LICENSE).

Not affiliated with, endorsed by, or sponsored by Signify (Philips Hue) or the Music Assistant project. *Hue* is a trademark of Signify Holding B.V.
