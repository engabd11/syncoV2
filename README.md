# Hue Synco for Home Assistant

Sync **Philips Hue Entertainment areas** to music from **Music Assistant**:
real-time, beat-accurate and eye-safe. Hue Synco ships as **one install**: the
integration streams colour to the bridge over the Hue Entertainment API
(~40 Hz over DTLS), and a bundled **dashboard card** is served automatically with
no separate download.

<p align="center">
  <img src="docs/card.png" alt="Hue Synco Card - Ambient Glow" width="360" />
</p>

> Create and arrange your entertainment areas in the **Hue app**; Hue Synco
> drives the areas that already exist.

## Features

- **Direct Entertainment streaming** (~40 Hz DTLS): fluid, high-rate, bypasses
  normal Zigbee light commands.
- **Real-time beat and frequency analysis** of the audio; no microphone needed.
- **Predictive beat grid**: locks tempo and phase and anticipates each beat so
  the lights peak on the kick. Falls back to reactive sync on ambient/irregular
  material.
- **3D spatial choreography**: uses each lamp's real x/y/z position; a kick sends
  a wavefront sweeping the room, treble lives up high, bass down low.
- **Structure-aware**: detects builds, drops and breakdowns and choreographs them.
- **Album-art colours**: a vivid palette extracted from the cover (perceptual
  CIELAB clustering that rejects the background and uses only colours actually in
  the art), plus a full-spectrum Rainbow and 11 preset themes.
- **Effects**: Music (beat/frequency choreography), Movies (calm soundtrack
  backlight) and Fireworks (bursts on big beats).
- **Eye safety, enforced**: a non-bypassable stage caps whole-room flashing at the
  WCAG limit (3 flashes/sec), keeps a brightness floor, desaturates rapid red and
  clamps colour to the bulb gamut. See [Eye safety](#eye-safety).
- **Reliable**: auto-reconnect with heartbeat, a silence noise-gate, large-area
  packet splitting, one-area-at-a-time, and optional exact light-state restore on
  stop.
- **Bundled dashboard card**: recolours to the album and runs a beat-locked
  visualiser, using the integration's real palette and tempo.

## Eye safety

> **Photosensitivity warning.** Audio-reactive lighting fills much of your vision
> and can flash on aggressive content. If you, or anyone who may be in the room,
> has photosensitive epilepsy or is sensitive to flashing light, use the
> **Subtle** intensity or the **Movies** effect (both are guaranteed flash-free)
> and avoid the higher intensities.

Every frame passes through a non-bypassable final stage that no effect or setting
can defeat: a **whole-room flash limiter** (3 flashes/sec, the WCAG 2.3.1 limit),
a **brightness floor** (the room never strobes black), a **saturated-red guard**,
and **gamut clamping plus slew-limiting** so colour never pops. Subtle and Movies
are flash-free by construction. These guarantees are asserted by the test suite,
but they cannot account for every individual's sensitivity, so the warning stands.

## Requirements

- A **v2 (square) Hue bridge** with an Entertainment area created in the Hue app
  (the round v1 bridge does not support entertainment streaming).
- **Music Assistant** connected to Home Assistant. A **Snapcast** server is
  recommended for beat-accurate audio on any player (set its host in the options).
- Bundled **ffmpeg**, **numpy** and **cryptography** (standard on HAOS, Container
  and Supervised). The DTLS transport is pure-Python, so no external `openssl`.

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
| Timing offset | Nudge the lights earlier or later (ms) to align with the sound |

**Intensity** sets how the lights move (dimming range, beat reactivity) relative
to the brightness ceiling: from **Subtle** (seamless colour drift, no flashing)
through **High** and **Intense** (room-sweeping wavefronts) to **Extreme** (a dark
club: vivid colour beams snap to full on every beat). **Effect** swaps the
renderer: **Music** (default), **Movies** (calm soundtrack-following backlight with
a warm cinematic drift in quiet scenes) or **Fireworks**. **Colour** picks the
palette independently. Only one area streams at a time per install.

## Dashboard card

The **Ambient Glow** card is bundled and auto-registered, so it appears in the
dashboard card picker as **Hue Synco Card** with no manual resource step:

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
    media_player: media_player.living_room   # optional
```

While an area is syncing, its `switch` publishes `album_colors`, `bpm`,
`beat_anchor`, now-playing (`media_title` / `media_artist` / `media_image`),
playback position anchors and `source_player`. The card uses the integration's
**real** palette, tempo and beat phase, so its colours and beat-locked bars match
the lights instead of approximating. Attributes are written only when they change.

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
Snapcast / MA audio --ffmpeg--> PCM --> Analyzer (bands + beat + tempo)
                                              |
album cover --ffmpeg--> palette --> Effect engine (3D per-lamp colour)
                                              |  -> eye-safety stage ->
                          HueStream v2 frames --DTLS (pure-Python PSK)--> bridge
```

Audio is tapped from your **Snapcast** server (beat-accurate for any Music
Assistant player) or decoded from the Music Assistant stream and position-locked
to playback, so pauses, seeks and track changes stay aligned. Any MA player type
works, including **squeezelite / slimproto** (flow streams and non-FLAC output
codecs are detected automatically). A dropped DTLS channel reconnects with backoff.

## Development

The DSP, colour and encoder logic has no Home Assistant dependency and runs
without HA:

```bash
pip install pytest numpy
pytest tests/
```

Tests cover the HueStream encoder, palette and album-art extraction, beat
detection and the noise gate, the effect engine, the predictive beat grid and
structure detection, the 3D spatial renderer, and the eye-safety invariants (the
flash limiter holding the 3 flashes/sec ceiling at every intensity).

## Limitations

- Needs **Music Assistant** audio; arbitrary HA players without a tappable stream
  are not supported.
- Snapcast playback is aligned automatically (the server's buffer is read from
  the wire protocol); the timing offset remains as a fine trim for other player
  types or unusual light latency.
- Requires a **v2** Hue bridge; entertainment streaming is not available on v1.

## License

MIT
