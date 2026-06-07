# Hue Music Sync for Home Assistant

A custom Home Assistant integration that syncs **Philips Hue Entertainment areas**
to music played through **Music Assistant** — a self-hosted replacement for
Samsung's music sync. It streams colour directly to the bridge over the low-latency
Hue Entertainment API (~50 Hz over DTLS), reacts to the **beat** and **frequency
content** of the audio, and lights each bulb differently based on its position in
the area and the frequency band it represents.

> Manage and arrange the lights inside an entertainment area in the **Hue app**.
> This integration consumes the areas that already exist and drives them.

## Features

- **Direct Hue Entertainment streaming** — bypasses normal Zigbee light commands
  for fast, fluid, high-rate updates (~40 Hz over DTLS).
- **Real-time beat + frequency analysis** of the audio (no microphone, no extra
  hardware), tapped from your **Snapcast** server or Music Assistant stream.
- **Smart, non-uniform choreography** — bass lights thump on the kick, treble
  lights shimmer, colours spread spatially across the area.
- **Colour shifts with the beat** — the palette doesn't just drift on a timer; it
  steps forward on every kick, so the colour visibly grooves with the music.
- **Colour schemes**, including a vivid palette **extracted from the current
  album cover**, a full-spectrum **Rainbow**, plus Sunset / Ocean / Forest /
  Lavender / Ember / Aurora and the Philips Hue signature scenes (Tropical /
  Savanna / Blossom / Honolulu / Galaxy).
- **Effects** beyond the music choreography: **Fireworks** (bursts ignite on big
  beats and fade out in the palette colours).
- **Movie mode** — a calm, non-distracting preset whose brightness follows the
  soundtrack and whose colour comes from the film's artwork.
- **Self-healing** — a dropped DTLS channel auto-reconnects with backoff instead
  of silently stopping.
- Per-area **switch** to activate/deactivate, plus **select/number** entities for
  intensity, effect, colour, master brightness, timing offset and followed player
  — and `hue_music_sync.activate` / `deactivate` / `set_options` **services** for
  automations.

## Requirements

- A **v2 (square) Philips Hue bridge** with at least one Entertainment area
  created in the Hue app. (The round v1 bridge does not support entertainment
  streaming.)
- **Music Assistant** running and connected to Home Assistant.
- *(Recommended)* a **Snapcast** server (Music Assistant streams through it) for
  beat-accurate audio on any player — set its host in the integration options.
- Home Assistant with the bundled **ffmpeg**, **numpy** and **cryptography**
  (all standard on HAOS, Container and Supervised installs). No external `openssl`
  binary is required — the DTLS channel is implemented in pure Python.

## Installation (HACS custom repository)

1. HACS → ⋮ → *Custom repositories* → add this repo, category **Integration**.
2. Install **Hue Music Sync**, then restart Home Assistant.
3. *Settings → Devices & Services → Add Integration → Hue Music Sync*.
4. Enter the **bridge IP**, **press the link button** on the bridge when prompted,
   then choose which **entertainment areas** to enable.

(Alternatively copy `custom_components/hue_music_sync/` into your HA `config/custom_components/` folder.)

Each enabled area becomes a device with a switch and three independent controls,
matching Samsung's model:

| Entity | Purpose |
| --- | --- |
| `switch.music_sync_<area>` | Activate / deactivate sync |
| `select` Intensity | How the lights *behave*: `Subtle`, `Medium`, `High`, `Intense`, `Movie` |
| `select` Effect | The renderer style: `Music` (default) or `Fireworks` |
| `select` Colour | `Album colours`, `Rainbow`, or a preset theme (Sunset/Ocean/Forest/Lavender/Ember/Aurora + Hue scenes) |
| `number` Brightness | Master brightness ceiling (5–100%), separate from intensity |
| `number` Timing offset | Nudge the lights earlier/later (ms) to line up with the sound |

**Intensity** controls only *how the lights move* — how much they dim and how
hard they react to the beat — relative to the brightness ceiling:

- **Subtle** — no dimming; colours just drift slowly.
- **Medium** — stays bright; some lights pulse brighter on the beat.
- **High** — dims no lower than ~30% of the ceiling, with bright bass + treble beats.
- **Intense** — full dimming/brightening across the whole range, with treble shimmer.
- **Movie** — calm and non-distracting: brightness gently follows the soundtrack's
  loudness (no flashes), colours drift slowly through the artwork. See
  [Movie mode](#movie-mode).

**Brightness** sets the overall ceiling; intensity varies brightness below it.
At Subtle, brightness is simply the steady level.

**Effect** swaps the whole renderer while still drawing colours from the selected
palette: **Music** is the default beat/frequency choreography; **Fireworks** keeps
the area dark and ignites random bursts on the big beats that fade out in the
palette's colours (more/faster bursts at higher intensities).

**Colour** picks the palette independently — the current album art, the
full-spectrum Rainbow (which steps hue on the beat), or a smooth, easy-on-the-eyes
preset theme. The followed media player auto-detects the one that's playing
(override via the `activate` service if needed).

### Movie mode

Set **Intensity = Movie** with **Colour = Album colours** for an ambient backlight
that won't pull your eye from the screen:

- **Brightness follows the soundtrack's overall loudness** — loud scenes swell,
  quiet scenes fade — with no beat flashes or shimmer, eased slowly both ways so
  even explosions *swell* rather than strobe.
- **Colour comes from the film's artwork** (poster / now-playing image), drifting
  slowly and softened toward white.
- **Adjust the overall level** with the Brightness slider.

Movie brightness reacts to *real* audio, so the film's sound must be tappable
(routed through your Snapcast server or Music Assistant). With no tappable audio
it falls back to a gentle breathing glow in the artwork colours rather than true
soundtrack-reactive brightness.

> **One area at a time per bridge.** A Hue bridge can stream to only one
> entertainment area at a time. Activating a second area on the same bridge
> automatically takes over from the one already running.

### Smooth dimming

Colour is streamed in Hue's native **xy chromaticity + a dedicated brightness
channel** (HueStream colourspace `0x01`) at ~40 Hz, the same model the official
Spotify/Samsung integrations use. Keeping brightness on its own channel (instead
of shrinking RGB magnitudes) lets the bridge map dimming through the bulb's own
smooth curve, so fades don't step at the low end.

## Usage

1. Start playback in Music Assistant.
2. Turn on the area's **Music Sync** switch (or call `hue_music_sync.activate`).
3. Tune **Latency offset** so the flashes line up with the sound (speaker buffering
   means there is always some delay to dial out).

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

| Service | What it does |
| --- | --- |
| `hue_music_sync.activate` | Start sync for the targeted area(s); optionally set `mode` / `effect` / `colour` / `brightness` / `media_player` first |
| `hue_music_sync.deactivate` | Stop sync for the targeted area(s) |
| `hue_music_sync.set_options` | Change `mode` / `effect` / `colour` / `brightness` / `media_player` **live**, without restarting sync — e.g. switch the vibe mid-song |

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

Within any mode, lights are driven per-channel by **spatial position** and
**frequency band**: lights are ordered left-to-right and mapped across the
spectrum, so bass-side lights thump on the kick while treble-side lights react
to highs — they don't all behave or colour the same. On top of that, the palette
**advances on every beat** (weighted by the kick), so the colour itself moves
with the music instead of only scrolling on a timer.

## Validation spikes

Two self-contained scripts under `scripts/` let you de-risk the moving parts on
your own host before/while using the integration:

- `python scripts/spike_dtls.py --host <bridge-ip> --pair` then `--list` then a
  colour-cycle run — proves the Hue Entertainment DTLS transport works.
- `python scripts/spike_ma.py --url <stream-or-file>` — decodes audio with ffmpeg
  and runs the real analyzer, printing beats / tempo / per-band levels.

## How it works

```
Snapcast / MA audio ──ffmpeg──▶ PCM ──▶ Analyzer (bands + beat)
                                              │
album cover ──ffmpeg──▶ palette ──▶ Effect engine (per-channel colour)
                                              │
                          HueStream v2 frames ──DTLS (pure-Python PSK)──▶ Hue bridge
```

Audio is tapped live from your **Snapcast** server (beat-accurate for any Music
Assistant player) or decoded from the Music Assistant stream and
**position-locked** to the player's reported playback position, so pauses, seeks
and track changes stay aligned. A dropped DTLS channel reconnects automatically
with backoff.

## Development

The DSP / colour / encoder logic has no Home Assistant dependency and is covered
by a fast unit-test suite that runs without HA:

```bash
pip install pytest numpy
pytest tests/
```

These cover the HueStream frame encoder, palette sampling, album-art k-means, the
analyzer's beat detection, and the effect engine (beat-driven colour stepping,
Fireworks bursts and Movie-mode loudness tracking).

## Limitations

- Works with **Music Assistant** audio (the chosen beat source). Arbitrary HA
  players without an accessible stream are not supported.
- Perfect lip-sync is not possible due to player buffering — use the latency
  offset to align by ear.
- Requires a **v2** Hue bridge; entertainment streaming is not available on v1.
- The DTLS channel is a self-contained pure-Python DTLS 1.2 PSK implementation
  (built on the bundled `cryptography`), since `python-mbedtls` has no modern
  wheels and the HA container ships no `openssl` CLI. It implements exactly one
  cipher suite (`TLS_PSK_WITH_AES_128_GCM_SHA256`) — enough for the bridge.

## License

MIT
