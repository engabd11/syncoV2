# Hue Synco for Home Assistant

Sync **Philips Hue Entertainment areas** to music from **Music Assistant**:
beat-accurate, theme-aware and eye-safe, with **every MA player type**
supported. Hue Synco ships as **one install**: the integration streams colour
to the bridge over the Hue Entertainment API (~40 Hz over DTLS), and a bundled
**dashboard card** is served automatically with no separate download.

<p align="center">
  <img src="docs/card.png" alt="Hue Synco Card - Ambient Glow" width="360" />
</p>

> Create and arrange your entertainment areas in the **Hue app**; Hue Synco
> drives the areas that already exist.

## Features

- **Direct Entertainment streaming** (~40 Hz DTLS): fluid, high-rate, bypasses
  normal Zigbee light commands.
- **Works with every Music Assistant player**: Snapcast, squeezelite, AirPlay,
  Chromecast, Sonos, DLNA, ESPHome, groups. No microphone needed anywhere.
- **It knows the song**: each track is analysed once in the background (the
  same approach as the official Hue+Spotify integration) into a *track map* -
  exact beat times from a full-track beat tracker, real downbeats, and the
  song's sections. Beats are *scheduled*, not guessed, and the choreography
  holds back in the verse so the chorus visibly arrives.
- **Kick-true triggering**: live analysis uses SuperFlux onset detection with a
  dedicated bass/kick stream, so vocals, hi-hats and vibrato no longer fire the
  lights - flashes, wavefronts and colour steps follow the actual pulse.
- **Automatic time alignment**: the Snapcast server's playout buffer is read
  from its wire protocol and compensated exactly, so the lights land on the
  audible beat at any tempo without manual trimming.
- **3D spatial choreography**: uses each lamp's real x/y/z position; a kick
  sends a wavefront sweeping the room, treble lives up high, bass down low.
- **Theme-faithful album colours**: the cover's palette is extracted in
  perceptual CIELAB space and keeps the album's *mood* - vivid accents plus the
  muted and dark theme tones (dark silver reads as a dim cool white, gold as a
  warm one), so a moody album gives a moody show. Plus a full-spectrum Rainbow
  and 11 preset themes.
- **Structure-aware**: builds tighten and desaturate, drops detonate a swell,
  breakdowns breathe - predictively when the track map knows what is coming.
- **Effects**: Music (beat/frequency choreography), Movies (calm soundtrack
  backlight with a warm cinematic drift) and Fireworks (bursts on big beats).
- **Eye safety, enforced**: a non-bypassable stage caps whole-room flashing at
  the WCAG limit (3 flashes/sec), keeps a brightness floor, desaturates rapid
  red and clamps colour to the bulb gamut. See [Eye safety](#eye-safety).
- **Reliable**: auto-reconnect with heartbeat, a silence noise-gate, large-area
  packet splitting, one-area-at-a-time, and optional exact light-state restore
  on stop.
- **Bundled dashboard card**: blurred album-art hero, palette-matched theme,
  beat-locked visualiser - driven by the integration's *real* palette, tempo
  and beat phase.

## Eye safety

> **Photosensitivity warning.** Audio-reactive lighting fills much of your
> vision and can flash on aggressive content. If you, or anyone who may be in
> the room, has photosensitive epilepsy or is sensitive to flashing light, use
> the **Subtle** intensity or the **Movies** effect (both are guaranteed
> flash-free) and avoid the higher intensities.

Every frame passes through a non-bypassable final stage that no effect or
setting can defeat: a **whole-room flash limiter** (3 flashes/sec, the WCAG
2.3.1 limit), a **brightness floor** (the room never strobes black), a
**saturated-red guard**, and **gamut clamping plus slew-limiting** so colour
never pops. Subtle and Movies are flash-free by construction. These guarantees
are asserted by the test suite, but they cannot account for every individual's
sensitivity, so the warning stands.

## Requirements

- A **v2 (square) Hue bridge** with an Entertainment area created in the Hue
  app (the round v1 bridge does not support entertainment streaming).
- **Music Assistant** connected to Home Assistant. A **Snapcast** server is
  optional but recommended (set its host in the options): it gives live,
  beat-accurate audio for any player it backs, including live radio.
- Bundled **ffmpeg**, **numpy** and **cryptography** (standard on HAOS,
  Container and Supervised). The DTLS transport is pure-Python, so no external
  `openssl`.

## Install (HACS)

1. HACS, then the menu, then *Custom repositories*: add this repo, category
   **Integration**.
2. Install **Hue Synco** and restart Home Assistant.
3. *Settings, Devices & Services, Add Integration, Hue Synco*: enter the bridge
   IP, press the bridge **link button**, and choose the entertainment areas.

Each area becomes a device with a **switch** plus the controls below.

## Controls

| Entity | Purpose |
| --- | --- |
| `switch` | Activate / deactivate sync |
| Intensity | `Subtle` / `Medium` / `High` / `Intense` / `Extreme` |
| Effect | `Music` / `Movies` / `Fireworks` |
| Colour | `Album colours` / `Rainbow` / a preset theme |
| Brightness | Master brightness ceiling, 5-100% |
| Timing offset | Fine trim, -500..+500 ms (alignment is otherwise automatic) |

**Intensity** sets how the lights move (dimming range, beat reactivity)
relative to the brightness ceiling: from **Subtle** (seamless colour drift, no
flashing) through **High** and **Intense** (room-sweeping wavefronts) to
**Extreme** (a dark club: vivid colour beams snap to full on every beat).
**Effect** swaps the renderer: **Music** (default), **Movies** (calm
soundtrack-following backlight) or **Fireworks**. **Colour** picks the palette
independently. Only one area streams at a time per install.

## Dashboard card

The **Ambient Glow** card is bundled and auto-registered, so it appears in the
dashboard card picker as **Hue Synco Card** with no manual resource step. It
finds the playing artwork and title by itself (via the `source_player` the
integration publishes), shows the album cover with a blurred-art hero
background, recolours its theme to the extracted palette, and runs a visualiser
locked to the real tempo and beat phase:

```yaml
type: custom:hue-music-sync-card
areas:
  - name: Living Room
    switch: switch.music_sync_living_room
    intensity: select.music_sync_living_room_intensity
    effect: select.music_sync_living_room_effect
    colour: select.music_sync_living_room_colour
    brightness: number.music_sync_living_room_brightness
    timing: number.music_sync_living_room_timing_offset
    media_player: media_player.living_room   # optional override
```

While an area is syncing, its `switch` publishes `album_colors`, `bpm`,
`beat_anchor`, `section_energy`, now-playing (`media_title` / `media_artist` /
`media_image`), playback position anchors and `source_player`. Attributes are
written only when they change, so the recorder stays quiet.

## Services

| Service | Description |
| --- | --- |
| `hue_music_sync.activate` | Start sync; optionally set `mode` / `effect` / `colour` / `brightness` / `media_player` first |
| `hue_music_sync.deactivate` | Stop sync |
| `hue_music_sync.set_options` | Change those settings live, without restarting |

All target the area's `switch`. Example:

```yaml
- service: hue_music_sync.set_options
  target:
    entity_id: switch.music_sync_living_room
  data:
    mode: extreme
```

## How it works

```
live audio (Snapcast / MA stream) --ffmpeg--> Analyzer (bands + kick onsets)
track audio (once, in background) --ffmpeg--> Track map (beats, sections)
                                                   |
album cover --ffmpeg--> theme palette --> Effect engine (3D per-lamp colour)
                                                   |  -> eye-safety stage ->
                             HueStream v2 frames --DTLS (pure-Python PSK)--> bridge
```

Two analysis paths feed the show. **Live**: audio is tapped from your Snapcast
server (auto-aligned to its playout buffer) or decoded from the Music Assistant
stream position-locked to playback (squeezelite / slimproto flow streams and
non-FLAC output codecs are detected automatically). **Offline**: each track is
also analysed once in the background into a *track map* - a full-track
dynamic-programming beat tracker, downbeats, sections and per-frame energies -
which then schedules the beats exactly and drives the section choreography.

Players with no tappable stream at all (**AirPlay, Chromecast, Sonos, DLNA,
ESPHome, groups, ...**) run entirely on the track map: the precomputed show is
replayed locked to the player's position. Runtime cost is an array lookup per
frame, and the beats come from the offline tracker, so these players are fully
beat-accurate too. A dropped DTLS channel reconnects with backoff.

| Player | Audio path | Beats |
| --- | --- | --- |
| Snapcast-backed (incl. live radio) | live snapserver tap, buffer-aligned | live + track map |
| squeezelite / slimproto | MA stream tap, position-locked | live + track map |
| AirPlay / Cast / Sonos / DLNA / ESPHome / groups | track-map playback | track map |
| Anything else playing in HA | metadata-driven animation | ambient |

## Development

The DSP, colour and encoder logic has no Home Assistant dependency and runs
without HA:

```bash
pip install pytest numpy
pytest tests/
```

Tests cover the HueStream encoder, theme-palette extraction, SuperFlux onset
detection (vibrato immunity, kick-vs-hihat discrimination), the offline track
map (beat tracking, sections, scheduled playback), the predictive beat grid and
structure detection, the effect engine, the 3D spatial renderer, and the
eye-safety invariants (the flash limiter holding the 3 flashes/sec ceiling at
every intensity). `python scripts/analyze_track.py <file>` dumps the track map
for a local song.

## Limitations

- Needs **Music Assistant**; non-MA players fall back to metadata-driven
  animation. Live radio on players without a tappable stream (nothing
  per-track to analyse) does too.
- Tracks whose per-track stream cannot be decoded (rare provider/DRM cases)
  fall back per-track and recover on the next song.
- Requires a **v2** Hue bridge; entertainment streaming is not available on v1.

## License

MIT
