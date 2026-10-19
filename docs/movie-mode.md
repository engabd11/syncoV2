# Movie mode (Hue Ghost)

Music sync covers music. For **films and TV** the same entertainment area can follow the screen instead, with a PC standing in for a Hue Sync Box. [Hue Ghost](https://github.com/engabd11/HueGhost) is a free Windows app that keeps a muted "ghost" copy of whatever your TV's Jellyfin client is playing in step with the TV, and the official Hue Sync desktop app captures that ghost and streams it to the area. From Hue Ghost 2.4.0 it also lights up for whatever is playing on the PC's own screen (a browser, a player, a game) and for Jellyfin music.

Hue Synco switches Hue Ghost on and off from Home Assistant and hands the area over cleanly, because a bridge allows one streamer per entertainment area.

## Set up

Set it up on the PC first (`hue-ghost setup` exposes its control API to the LAN with a token), then choose **Configure â†’ Hue Ghost PC host / port / token** in Hue Synco. A **Hue Ghost** device appears, with entities named for what each one does rather than for the device it sits under:

| Entity | Type | Description |
|---|---|---|
| Global sync | Light | **The master control.** On/off is movie mode, and the brightness slider is the level Hue Sync runs the area at, so it works in scenes, in voice assistants and on any light card. Turning it **on** stops every active music-sync area first, and starting a music-sync area turns it **off** |
| Sync status | Sensor | `offline` / `disabled` (Global sync off) / `idle` (on, waiting for playback) / `ghosting` / `syncing`: the one to trigger automations on. If the PC misses a poll or two it keeps its last state. After three missed polls in a row it is `offline` and Global sync reads *unknown* rather than a false *off*. Comes with what is playing, the measured ghost-vs-TV drift and the Hue Sync app state as attributes |
| Sync area | Sensor | Which entertainment area the sync plays in. Reported rather than set, because the area is chosen per source in Hue Ghost. The `areas_by_source` attribute lists where each followed source would go |
| Active source | Sensor | Which followed source is driving the lights *now*, a different question from which ones are switched on |
| Now playing | Sensor | The Jellyfin item the TV is playing, or whatever the PC source reported (a window title, a game) |
| Sync intensity | Select | Hue Sync's intensity (subtle / moderate / high / extreme), applied when a movie starts and live while syncing |
| Sync mode | Select | What Hue Sync reacts to: video (the picture), music (the sound) or games. Applies live, mid-movie |
| *&lt;source&gt;* | Switch | One per thing Hue Ghost can follow, a Jellyfin client or an app on the PC, named for the source itself ("Apple TV", "PC"). Off ignores the source while keeping it in the list |
| Audio effects | Switch | Hue Sync's *use audio for light effects* for video and games mode. Until you touch it, it mirrors what the Hue Sync app itself is set to. Flipping it hands the setting to Hue Ghost, which applies it at the start of the next sync |
| Film sync offset | Number | How far the ghost runs ahead of the TV to cancel capture, bridge and lamp latency, for films, TV and PC video. Lights late? Raise it. Lights early? Lower it |
| Sync drift | Sensor | *(diagnostic)* The measured gap between the ghost and the TV, in seconds, which is the evidence for the offset above |

With movie mode on, everything else is automatic: press play on the TV and the lights follow, and when you stop, they stop. The PC's Hue Sync app needs *Allow public control* enabled (see the [Hue Ghost README](https://github.com/engabd11/HueGhost#readme)).

Every Hue Ghost entity also carries a **`description`** attribute: one plain sentence on what it does and what its values mean. Open the entity and look under **Attributes**.

## Remote control (Hue Ghost 2.13 and newer)

The per-source controls are named *Sync area for …*, *Sync mode for …* and *Sync intensity for …*, so they sit together in the entity list and say what they set. (1.62.0 called them *&lt;source&gt; area* and so on; entities you already have keep their entity ids.)

With Hue Ghost 2.13 or newer on the PC, Home Assistant can change almost everything the PC app can, so a dashboard or the phone app is a remote for it. Older versions show these entities as unavailable.

### What syncs, and where

| Entity | Type | Description |
|---|---|---|
| Sync source | Select | The source to sync now. **Automatic**: the first source that plays wins, in the order set in the Hue Ghost app. Pick a source to put it first whenever it plays; the others still sync while it does not. Picking a source that is switched off switches it on |
| Sync area for *&lt;source&gt;* | Select | The entertainment area this source syncs in. *Hue Sync's current area* follows whatever the Hue Sync app is set to. Changing it while the source plays moves the lights there (Hue Sync restarts for a few seconds) |
| Sync mode for *&lt;source&gt;* | Select | What Hue Sync reacts to while this source plays: Video, Music or Games. *Default* uses Sync mode |
| Sync intensity for *&lt;source&gt;* | Select | How strongly the lights react while this source plays. *Default* uses Sync intensity |

A paused source never holds the lights while another one plays, and it takes them back as soon as it plays again.

### Timings

All in **Configuration** on the Hue Ghost device.

| Entity | Unit | What it does |
|---|---|---|
| Film sync offset | seconds | Moves the lights against the TV picture. Lights late? Raise it. Lights early? Lower it |
| Music sync offset | seconds | The same for music, which travels a different path (Hue Sync listens instead of watching). Lights after the beat? Raise it. Until it is set, music uses the film offset, and the entity shows that value with `shared_with_films: true` |
| Lights off after video stops | seconds | How long after a film, TV show or PC app stops before the lights stop syncing. Short brings the room back at once; a little longer rides out a player that stops and starts again |
| Standby after a film stops | seconds | How long Hue Ghost keeps its hidden copy of the film ready. Pressing play again within this time continues at once, without reloading. The lights are already off by then |
| Gap allowed between songs | seconds | How long music may stop between tracks before the lights stop syncing |
| Music pause limit | seconds | How long music may stay paused before the lights stop. Play starts them again. **0** = never stop for a pause |
| Film pause limit | minutes | How long a film or TV show may stay paused before the lights stop. Play starts them again. **0** = never stop for a pause |
| Screen care: pixel shift every | minutes | For OLED screens kept awake: moves the hidden film picture a few pixels this often. **0** = off |
| Screen care: black out idle screens after | minutes | Covers the PC's screens with black after this long without mouse or keyboard use, while they are kept awake. **0** = off |

### The rest

| Entity | Type | Description |
|---|---|---|
| Time lock | Switch | Once the lights are in step with the player, hold that timing instead of correcting it, so they play together rather than chase. Seeks, pauses and new songs or episodes still re-align them. Leave it on |
| Keep displays awake | Select | Windows switching the displays off stops Hue Sync seeing the film. **Off**, **While a film syncs** (music lets the screens sleep and keeps only the PC awake) or **Always** |
| Restart app | Button | Restarts Hue Ghost on the PC. It is back within about ten seconds |

## Versions

Mode, intensity and the audio switch need **Hue Ghost 2.3.0** or newer. The brightness light and the per-source switches need **2.4.0**. On older versions the light still switches movie mode, and the level, the source switches and the remaining entities appear once Hue Ghost is upgraded. You choose the entertainment area per source in Hue Ghost, and Hue Ghost applies it in the Hue Sync app when a movie starts.

> **Upgrading from 1.59 or earlier.** The separate `switch.hue_ghost_movie_mode` is gone, because the Global sync light already did both halves of its job. It is removed from the registry on the first restart, so point any card or automation at the light instead. Entities you already have keep their old entity ids (Home Assistant keeps entity ids stable once created), and the new names show up as friendly names. To take the shorter ids as well, delete the Hue Ghost device under **Settings â†’ Devices & services â†’ Hue Synco** and reload the entry. It comes straight back with `light.hue_ghost_global_sync`, `sensor.hue_ghost_sync_status` and so on.

## The Hue Ghost card

The bundled **Hue Ghost Card (Movie mode)** puts all of it in one tile: the light and its level, the intensity and mode pickers, and a tile per source, with the one that is playing ringed in gold. It uses the Hue Ghost desktop app's own warm charcoal and gold, so the dashboard and the PC look like one product. It is served by the integration like the music-sync card, so it installs with the integration, and it finds the Hue Ghost device by itself:

```yaml
type: custom:hue-ghost-card
```

That is the whole config. Everything below is optional:

| Option | Default | |
|---|---|---|
| `title` | the device name | Heading text |
| `icon` | `mdi:ghost` | Heading icon |
| `show_intensity`, `show_mode`, `show_sources` | `true` | Hide a section |
| `show_sync_source`, `show_area` | `true` | Hide the **Sync source** dropdown, or the **area** dropdown of the source in charge (the one playing, else the one picked). Both need Hue Ghost 2.13 |
| `show_settings` | `true` | Hide the gear button and its settings panel |
| `show_descriptions` | `true` | Hide the one-line explanation under each setting in the panel |

### The settings panel

With Hue Ghost 2.13 or newer, the **Settings** bar at the bottom of the card (or the **gear** next to the power switch) expands the rest of the remote on the same card, and retracts it again. Each part inside folds open and shut from its own heading, and the card remembers what you left open in that browser:

- **Each source:** its area, mode and intensity side by side, with the one playing ringed in gold.
- **Timings:** every timing from the table above, with **âˆ’** and **+** buttons, its unit, and the same one-line explanation as the entity's `description`.
- **More:** the time lock, keeping the displays awake, and a button that restarts Hue Ghost on the PC.

Nothing in it needs configuring: it finds every control on the Hue Ghost device, including sources added later on the PC.
| `light`, `status`, `area`, `source`, `playing`, `intensity`, `mode`, `sources` | auto | Pin a specific entity, for example with two PCs |

Because it reads the sources from the device, a source added in the Hue Ghost app appears as a new tile on its own.
