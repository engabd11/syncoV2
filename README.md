# Hue Music Sync for Home Assistant

A custom Home Assistant integration that syncs **Philips Hue Entertainment areas**
to music played through **Music Assistant**, a self-hosted replacement for
Samsung's Hue music sync. It streams colour directly to the bridge over the
Hue Entertainment API (~40 Hz over DTLS), reacts to the beat and frequency
content of the audio, and lights each bulb based on its position in the area
and the frequency band it represents.

Manage and arrange the lights inside an entertainment area in the **Hue app**.
This integration consumes the areas that already exist and drives them.

## Features

- **Direct Hue Entertainment streaming** over DTLS for low-latency, high-rate
  updates (~40 Hz), bypassing normal Zigbee light commands.
- **Real-time beat and frequency analysis** from your Snapcast server or Music
  Assistant stream — no microphone required.
- **Non-uniform choreography**: bass lights thump on the kick, treble lights
  shimmer, colours spread spatially across the area.
- **Beat-driven colour shifts**: the palette steps forward on every kick so the
  colour visibly grooves with the music instead of drifting on a timer.
- **13 colour schemes**: album art extraction (k-means palette from the current
  cover), a full-spectrum Rainbow, and 11 preset themes (Sunset, Ocean, Forest,
  Lavender, Ember, Aurora, plus the Philips Hue signature scenes: Tropical,
  Savanna, Blossom, Honolulu, Galaxy).
- **Three effects**: Music (beat/frequency choreography), Movies (calm
  soundtrack-following backlight whose brightness tracks the audio and colour
  comes from the film's artwork), and Fireworks (bursts ignite on big beats and
  fade out in the palette colours).
- **Self-healing DTLS**: a dropped channel auto-reconnects with exponential
  backoff instead of silently stopping.
- **Per-area entities**: an on/off switch, plus select entities for Intensity,
  Effect, and Colour, and number entities for Brightness and Timing offset.
- **Services** for automation: `hue_music_sync.activate`, `deactivate`,
  and `set_options` (change settings live without restarting).

## Requirements

- A **v2 (square) Philips Hue bridge** with at least one Entertainment area
  created in the Hue app. The round v1 bridge does not support entertainment
  streaming.
- **Music Assistant** running and connected to Home Assistant.
- **Snapcast** server (recommended) for beat-accurate audio on any player.
  Set its host in the integration options.
- Home Assistant with the bundled **ffmpeg**, **numpy**, and **cryptography**
  libraries (standard on HAOS, Container, and Supervised installs). No external
  `openssl` binary is required; the DTLS channel is implemented in pure Python.

## Installation (HACS custom repository)

1. HACS → ⋮ → *Custom repositories* → add this repo, category **Integration**.
2. Install **Hue Music Sync**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Hue Music Sync*.
4. Enter the **bridge IP**, press the **link button** on the bridge when
   prompted, then choose which **entertainment areas** to enable.

You can also copy `custom_components/hue_music_sync/` directly into your HA
`config/custom_components/` folder.

Each enabled area becomes a device with the following controls:

| Entity | Purpose |
| --- | --- |
| `switch.music_sync_<area>` | Activate / deactivate sync |
| `select` Intensity | How the lights behave: `Subtle`, `Medium`, `High`, `Intense` |
| `select` Effect | Renderer style: `Music`, `Movies`, `Fireworks` |
| `select` Colour | `Album colours`, `Rainbow`, or a preset theme |
| `number` Brightness | Master brightness ceiling (5–100%) |
| `number` Timing offset | Nudge the lights earlier or later (ms) to align with the sound |

**Intensity** controls only how the lights move — their dimming range and beat
reactivity relative to the brightness ceiling:

- **Subtle**: no dimming; colours drift slowly.
- **Medium**: stays bright; some lights pulse on the beat.
- **High**: dims to ~30% of the ceiling, with bright bass and treble beats.
- **Intense**: full dimming/brightening across the whole range, with treble
  shimmer.

**Brightness** sets the overall ceiling; intensity varies brightness below it.
At Subtle, brightness is simply the steady level.

**Effect** swaps the renderer while keeping the selected colour palette:

- **Music**: default beat/frequency choreography.
- **Movies**: calm soundtrack-following backlight (see below).
- **Fireworks**: keeps the area dark and ignites random bursts on big beats
  that fade out in the palette colours. More and faster bursts at higher
  intensities.

Intensity applies to Music and Fireworks; Movies stays gentle regardless (use
the Brightness slider to set its level).

**Colour** picks the palette independently: the current album art, the
full-spectrum Rainbow (which steps hue on the beat), or a smooth preset theme.
The followed media player auto-detects the one that's playing; override it
via the `activate` service.

### Movies effect

Set **Effect = Movies** with **Colour = Album colours** for an ambient
backlight that won't distract from the screen:

- **Brightness follows the soundtrack's overall loudness**: loud scenes swell,
  quiet scenes fade, with no beat flashes or shimmer. Even explosions swell
  rather than strobe.
- **Colour comes from the film's artwork** (poster / now-playing image),
  drifting slowly and softened toward white.
- **Adjust the overall level** with the Brightness slider.

Movie brightness reacts to real audio, so the film's sound must be tappable
(routed through your Snapcast server or Music Assistant). Without tappable
audio it falls back to a gentle breathing glow in the artwork colours.

One area at a time per bridge: activating a second area on the same bridge
automatically takes over from the one already running.

### Smooth dimming

Colour is streamed in Hue's native **xy chromaticity with a dedicated
brightness channel** (HueStream colourspace `0x01`) at ~40 Hz, the same model
the official Spotify/Samsung integrations use. Keeping brightness on its own
channel lets the bridge map dimming through the bulb's own smooth curve, so
fades don't step at the low end.

## Usage

1. Start playback in Music Assistant.
2. Turn on the area's **Music Sync** switch (or call `hue_music_sync.activate`).
3. Adjust the **Timing offset** so the flashes line up with the sound; speaker
   buffering always introduces some delay.

Example automation — start sync when Music Assistant begins playing:

```yaml
automation:
  - alias: Music sync on play
    trigger:
      - platform: state
        entity_id: media_player.living_room
        to: playing
    action:
      - service: hue_music_sync.activate
        target:
          entity_id: switch.music_sync_living_room
        data:
          mode: high
          colour: album_art
```

## Services

| Service | Description |
| --- | --- |
| `hue_music_sync.activate` | Start sync for the targeted area(s); optionally set `mode`, `effect`, `colour`, `brightness`, or `media_player` first |
| `hue_music_sync.deactivate` | Stop sync for the targeted area(s) |
| `hue_music_sync.set_options` | Change `mode`, `effect`, `colour`, `brightness`, or `media_player` live, without restarting sync |

All three target the area's `switch` entity. Example — go full Intense on the drop:

```yaml
- service: hue_music_sync.set_options
  target:
    entity_id: switch.music_sync_living_room
  data:
    mode: intense
    effect: music
```

## Choreography

Lights are driven per-channel by **spatial position** and **frequency band**:
lights are ordered left-to-right and mapped across the spectrum, so bass-side
lights thump on the kick while treble-side lights react to highs. The palette
advances on every beat (weighted by the kick), so the colour moves with the
music instead of scrolling on a timer.

## Validation spikes

Two self-contained scripts under `scripts/` let you verify the moving parts on
your own host before using the integration:

- `python scripts/spike_dtls.py --host <bridge-ip> --pair` then `--list` then a
  colour-cycle run — proves the Hue Entertainment DTLS transport works.
- `python scripts/spike_ma.py --url <stream-or-file>` — decodes audio with
  ffmpeg and runs the real analyzer, printing beats, tempo, and per-band levels.

## How it works

```
Snapcast / MA audio --ffmpeg--> PCM --> Analyzer (bands + beat)
                                             |
album cover --ffmpeg--> palette --> Effect engine (per-channel colour)
                                             |
                         HueStream v2 frames --DTLS (pure-Python PSK)--> Hue bridge
```

Audio is tapped live from your **Snapcast** server (beat-accurate for any Music
Assistant player) or decoded from the Music Assistant stream and
**position-locked** to the player's reported playback position, so pauses,
seeks, and track changes stay aligned. A dropped DTLS channel reconnects
automatically with backoff.

## Development

The DSP, colour, and encoder logic has no Home Assistant dependency and is
covered by a fast unit-test suite that runs without HA:

```bash
pip install pytest numpy
pytest tests/
```

Tests cover the HueStream frame encoder, palette sampling, album-art k-means,
the analyzer's beat detection, and the effect engine (beat-driven colour
stepping, Fireworks bursts, and Movie-mode loudness tracking).

## Limitations

- Works with **Music Assistant** audio (the chosen beat source). Arbitrary HA
  players without an accessible stream are not supported.
- Perfect lip-sync is not possible due to player buffering; use the latency
  offset to align by ear.
- Requires a **v2** Hue bridge; entertainment streaming is not available on v1.
- The DTLS channel is a self-contained pure-Python DTLS 1.2 PSK implementation
  built on the bundled `cryptography` library, implementing exactly one cipher
  suite (`TLS_PSK_WITH_AES_128_GCM_SHA256`), which is all the bridge requires.

## License

MIT
