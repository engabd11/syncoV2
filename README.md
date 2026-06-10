# Hue Music Sync for Home Assistant

Syncs **Philips Hue Entertainment areas** to music played through **Music Assistant**.
Streams colour directly to the bridge over the Hue Entertainment API
(~40 Hz over DTLS). Reacts to the beat and frequency content of the audio,
lighting each bulb based on its position in the area and the frequency band
it represents.

Manage and arrange the lights inside an entertainment area in the **Hue app**.
This integration consumes the areas that already exist and drives them.

## Features

- **Direct Hue Entertainment streaming** over DTLS for low-latency, high-rate
  updates (~40 Hz), bypassing normal Zigbee light commands.
- **Real-time beat and frequency analysis** from your Snapcast server or Music
  Assistant stream; no microphone required.
- **Predictive beat grid**: locks onto the tempo and phase of the music and
  anticipates each beat, so the lights peak on the kick instead of chasing it.
  Falls back to plain reactive sync on irregular or ambient material.
- **3D spatial choreography**: uses each lamp's real position in the room, not
  just left to right. A kick launches a wavefront that sweeps across the room,
  treble lives up high and bass down low, and the back lamps carry an ambient
  wash; motion that LED strips can't do.
- **Structure awareness**: detects builds, drops, and breakdowns; tension
  desaturates and tightens through a riser, then the whole field swells on the
  drop.
- **Non-uniform choreography**: bass lights thump on the kick, treble lights
  shimmer, colours spread spatially across the area.
- **Beat-driven colour shifts**: the palette steps forward on every kick so the
  colour visibly grooves with the music instead of drifting on a timer.
- **13 colour schemes**: a vivid palette **extracted from the current album
  cover** (perceptual CIELAB clustering that rejects the grey/black background
  and ranks colours by vividness rather than raw size, using only colours
  actually in the cover and ordering them for a smooth hue drift), a full-spectrum
  Rainbow, and 11 preset themes (Sunset, Ocean, Forest, Lavender, Ember, Aurora,
  plus the Philips Hue signature scenes: Tropical, Savanna, Blossom, Honolulu,
  Galaxy).
- **Three effects**: Music (beat/frequency choreography), Movies (calm
  soundtrack-following backlight whose brightness tracks the audio and colour
  comes from the film's artwork), and Fireworks (bursts ignite on big beats and
  fade out in the palette colours).
- **Eye safety, enforced**: a non-bypassable final stage caps whole-room flashing
  at the WCAG limit (3 flashes/sec), keeps a brightness floor so the room never
  strobes black, desaturates rapid saturated-red, clamps every colour to the bulb
  gamut, and slew-limits colour moves so nothing pops. See [Eye safety](#eye-safety).
- **Self-healing DTLS**: a dropped channel auto-reconnects with exponential
  backoff instead of silently stopping, with a heartbeat so the area never drops
  on quiet passages and a noise gate so true silence rests. If a channel can't be
  recovered the area is handed straight back to the bridge, which restores the
  prior light state immediately rather than leaving the lamps frozen.
- **Large-area safe**: frames are split into packets of at most 10 lights (the
  bridge's per-packet limit), so big setups with several lamps plus gradient-strip
  segments stream reliably instead of dropping over-stuffed datagrams.
- **Per-area entities**: an on/off switch, plus select entities for Intensity,
  Effect, and Colour, and number entities for Brightness and Timing offset.
- **Services** for automation: `hue_music_sync.activate`, `deactivate`,
  and `set_options` (change settings live without restarting).

## Eye safety

> **Photosensitivity warning.** Audio-reactive lighting fills a large part of
> your vision and can, on aggressive content, flash. If you, or anyone who may be
> in the room, has photosensitive epilepsy or is sensitive to flashing light, use
> the **Subtle** intensity or the **Movies** effect (both are guaranteed
> flash-free) and avoid the higher intensities.

Eye safety is a first-class feature, not an afterthought. Every frame, from any
effect, intensity, colour, or the idle glow, passes through a **non-bypassable
final safety stage** before it reaches the bridge:

- **Whole-field flash limiter.** No effect or setting can make the whole room
  flash more than **3 times per second** (the WCAG 2.3.1 "three flashes" limit).
  The limiter is transparent on normal content and only engages on genuine
  strobing, which it tames by compressing the global brightness swing while
  preserving each light's colour and the spatial pattern between lights.
- **Brightness floor.** Reactive modes pulse up over a non-zero baseline and decay
  down; the room never fully extinguishes and re-ignites (which reads as
  strobing). Even explosions in Movies swell rather than flash.
- **Saturated-red guard.** Deep red has a stricter flash threshold, so rapid red
  flashing is automatically desaturated toward white.
- **Calm presets.** **Subtle** and **Movies** contain zero discrete flashes by
  construction; only eased, sub-1-Hz drifts. This is enforced by unit tests, not
  left to chance.

These are guarantees the test suite asserts on worst-case (aggressive EDM) input.
They make the integration safe to leave running in a shared space, but they cannot
account for every individual's sensitivity, so the warning above still applies.

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
| `select` Intensity | How the lights behave: `Subtle`, `Medium`, `High`, `Intense`, `Extreme` |
| `select` Effect | Renderer style: `Music`, `Movies`, `Fireworks` |
| `select` Colour | `Album colours`, `Rainbow`, or a preset theme |
| `number` Brightness | Master brightness ceiling (5–100%) |
| `number` Timing offset | Nudge the lights earlier or later (ms) to align with the sound |

**Intensity** controls only how the lights move: their dimming range and beat
reactivity relative to the brightness ceiling:

- **Subtle**: seamless. The lights hold a steady, bright level and just let the
  colour flow and shift smoothly across the room; no flashing, no beat stepping.
- **Medium**: gentle. Stays bright and sways softly with the music, colour
  drifting with a small nudge on each beat; still no flashing or wavefronts.
- **High**: dims to ~30% of the ceiling; the kick sweeps the room as a wavefront.
- **Intense**: full dimming/brightening across the whole range, strong wavefronts
  and treble shimmer.
- **Extreme**: the maximum, a real club. The room sits dark and vivid colour
  beams snap to full and sweep across the lamps on every beat, with hard fast
  colour jumps and treble sparkle — distinctly harder than Intense's lit,
  immersive baseline. The eye-safety stage holds whole-room flashing under the
  WCAG limit, so the energy stays in the moving beams and the colour rather than
  the whole room strobing.

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
- **Warm cinematic drift**: in quiet scenes the colour eases toward a cosy
  tungsten white and back to the artwork hue as the scene swells, so dialogue
  moments feel like candlelight rather than a tinted wash.
- **Adjust the overall level** with the Brightness slider.

Movie brightness reacts to real audio, so the film's sound must be tappable
(routed through your Snapcast server or Music Assistant). Without tappable
audio it falls back to a gentle breathing glow in the artwork colours.

One area at a time: only a single entertainment area streams at once (a bridge
supports one stream, and the integration enforces this across every area and
bridge). Activating any area automatically deactivates whichever one was running.

### Restoring light state on stop

When sync stops, the bridge restores the lights to their pre-sync state, but it
occasionally drops a single light's restore (a Zigbee quirk) and leaves it on its
last colour. Enable **Restore exact light state when sync stops** in the
integration options to have the integration snapshot each light before streaming
and re-apply that exact state itself (with a retry) after stopping. It is off by
default since it adds a few bridge calls on start/stop; turn it on if you see a
light left behind.

### Smooth dimming & deterministic colour

Colour is streamed in Hue's native **xy chromaticity with a dedicated
brightness channel** (HueStream colourspace `0x01`) at ~40 Hz, the same model
the official Spotify/Samsung integrations use. Keeping brightness on its own
channel lets the bridge map dimming through the bulb's own smooth curve, so
fades don't step at the low end.

The bridge sends each frame straight to the bulbs without interpolating between
them, so the stream itself is the only smoothing a bulb gets. Two things keep it
clean and predictable:

- **Gamut clamping.** Every colour is clamped to the bulb's reproducible gamut
  (Gamut C) on our side, so colour is deterministic instead of being snapped
  unpredictably by the bridge.
- **xy slew-limiting.** A large colour jump (palette switch, new album art, a
  Rainbow beat-step) is capped to a smooth per-frame move, so colour *grooves*
  to the next hue instead of popping.

## Usage

1. Start playback in Music Assistant.
2. Turn on the area's **Music Sync** switch (or call `hue_music_sync.activate`).
3. Adjust the **Timing offset** so the flashes line up with the sound; speaker
   buffering always introduces some delay.

Example automation to start sync when Music Assistant begins playing:

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

### Dashboard card attributes

While an area is syncing, its `switch` exposes extra state attributes so a
dashboard card (e.g. the companion
[Hue Music Sync Card](https://github.com/engabd11/hue-music-sync-card)) can
reflect what's playing — recolour to the album and lock a visualizer to the song:

| Attribute | Meaning |
| --- | --- |
| `album_colors` | The extracted album palette as `#rrggbb` hex (when **Colour = Album colours**). |
| `bpm` | Detected tempo, once the rhythm model locks. |
| `media_title` / `media_artist` / `media_image` | Now-playing track, artist and album art of the followed player. |
| `source_player` | The `media_player` entity the sync is following. |

The attributes are only written when they change (per track / on tempo lock), so
they don't spam the state machine or recorder, and they disappear when the area
stops syncing.

## Services

| Service | Description |
| --- | --- |
| `hue_music_sync.activate` | Start sync for the targeted area(s); optionally set `mode`, `effect`, `colour`, `brightness`, or `media_player` first |
| `hue_music_sync.deactivate` | Stop sync for the targeted area(s) |
| `hue_music_sync.set_options` | Change `mode`, `effect`, `colour`, `brightness`, or `media_player` live, without restarting sync |

All three target the area's `switch` entity. Example: go full Intense on the drop:

```yaml
- service: hue_music_sync.set_options
  target:
    entity_id: switch.music_sync_living_room
  data:
    mode: intense
    effect: music
```

## Choreography

Within any mode, lights are driven per-channel by **3D position** and **frequency
band**. Each lamp's real place in the room matters:

- **Left to right** maps across the spectrum and pans the colour.
- **Height** sets which band a lamp favours: bass on the floor, treble at the
  ceiling.
- **Depth** (front to back) splits a reactive front from an ambient back wash.

On a beat, the kick launches a **wavefront** that expands from the low centre of
the room and sweeps outward across the lamps: spatial motion rather than every
lamp flashing in unison (which both looks better and is easier on the eyes). The
palette also advances on every beat (weighted by the kick) so the colour itself
grooves with the music.

## Musical intelligence

The sync doesn't just react to the last 20 ms of sound; it builds a model of the
music:

- **Predictive beat grid.** A tempo and phase tracker (autocorrelation of the
  onset envelope with a log-tempo prior to avoid half/double-tempo errors, plus a
  phase-locked loop) predicts when the next beat will land. The lights are fired a
  configurable few milliseconds early so they peak on the kick instead of lagging
  it. When the music is irregular or ambient and the tempo won't lock, it silently
  falls back to plain reactive sync.
- **Structure awareness.** Slow envelopes of loudness and spectral brightness spot
  builds (tension rising, the field tightens and desaturates), drops (the release,
  the whole field swells), and breakdowns.

Because the integration taps and decodes the audio stream itself (rather than a
microphone or HDMI feed), it can also analyse *ahead* of the audible playback
position: a true look-ahead that mic/HDMI sync can't do. The renderer already
accepts a future-frames slice for this; wiring the decoder to run ahead is the
next step (the `scripts/spike_ma.py` headroom check gates it). Predictive beat
timing above needs no look-ahead; it predicts from the rhythm model.

## Validation spikes

Two self-contained scripts under `scripts/` let you verify the moving parts on
your own host before using the integration:

- `python scripts/spike_dtls.py --host <bridge-ip> --pair` then `--list` then a
  colour-cycle run; proves the Hue Entertainment DTLS transport works.
- `python scripts/spike_ma.py --url <stream-or-file>`; decodes audio with
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

The Music Assistant tap works with any MA player type — including **squeezelite /
slimproto** players, which stream the whole queue as one continuous *flow* and
can use a non-FLAC output codec. The integration builds the stream URL from the
player's actual flow mode and codec, and falls through the other variants if the
first doesn't decode, so it finds the right stream automatically.

## Development

The DSP, colour, and encoder logic has no Home Assistant dependency and is
covered by a fast unit-test suite that runs without HA:

```bash
pip install pytest numpy
pytest tests/
```

Tests cover the HueStream frame encoder, palette sampling, album-art k-means, the
analyzer's beat detection and noise gate, the effect engine (beat-driven colour
stepping, Fireworks bursts, and Movie-mode loudness tracking), the predictive beat
grid (`tests/test_tempo.py`: tempo lock across 90-174 BPM, phase alignment to real
onsets, graceful unlock on noise, tempo-change re-lock), structure detection
(`tests/test_structure.py`: build to drop, no false triggers), the 3D spatial
renderer (`tests/test_spatial.py`: geometry, wave propagation), and the eye-safety
invariants (`tests/test_safety.py`): the flash limiter holds the WCAG 3 flashes/sec
ceiling on aggressive input at every intensity (re-asserted over the full
predictive and spatial pipeline), Subtle and Movies are flash-free by
construction, saturated-red strobing desaturates, and every colour stays inside
the bulb gamut.

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
