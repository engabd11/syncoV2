# Movie mode (Hue Ghost)

Music sync covers music. For **films and TV** the same entertainment area can follow the screen instead, with a PC standing in for a Hue Sync Box. [Hue Ghost](https://github.com/engabd11/HueGhost) is a free Windows app that keeps a muted "ghost" copy of whatever your TV's Jellyfin client is playing in step with the TV, and the official Hue Sync desktop app captures that ghost and streams it to the area. From Hue Ghost 2.4.0 it also lights up for whatever is playing on the PC's own screen (a browser, a player, a game) and for Jellyfin music.

Hue Synco switches Hue Ghost on and off from Home Assistant and hands the area over cleanly, because a bridge allows one streamer per entertainment area.

## Set up

Set it up on the PC first (`hue-ghost setup` exposes its control API to the LAN with a token), then choose **Configure → Hue Ghost PC host / port / token** in Hue Synco. A **Hue Ghost** device appears, with entities named for what each one does rather than for the device it sits under:

| Entity | Type | Description |
|---|---|---|
| Global sync | Light | **The master control.** On/off is movie mode, and the brightness slider is the level Hue Sync runs the area at, so it works in scenes, in voice assistants and on any light card. Turning it **on** stops every active music-sync area first, and starting a music-sync area turns it **off** |
| Sync status | Sensor | `offline` / `disabled` (Global sync off) / `idle` (on, waiting for playback) / `ghosting` / `syncing`: the one to trigger automations on. If the PC misses a poll or two it keeps its last state. After three missed polls in a row it is `offline` and Global sync reads *unknown* rather than a false *off*. Comes with what is playing, the measured ghost-vs-TV drift and the Hue Sync app state as attributes |
| Sync area | Sensor | Which entertainment area the sync plays in. Reported rather than set, because the area is chosen per source in Hue Ghost. The `areas_by_source` attribute lists where each followed source would go |
| Active source | Sensor | Which followed source is driving the lights *now*, a different question from which ones are switched on |
| Now playing | Sensor | The Jellyfin item the TV is playing, or whatever the PC source reported (a window title, a game) |
| Intensity | Select | Hue Sync's intensity (subtle / moderate / high / extreme), applied when a movie starts and live while syncing |
| Mode | Select | What Hue Sync reacts to: video (the picture), music (the sound) or games. Applies live, mid-movie |
| *&lt;source&gt;* | Switch | One per thing Hue Ghost can follow, a Jellyfin client or an app on the PC, named for the source itself ("Apple TV", "PC"). Off ignores the source while keeping it in the list |
| Audio effects | Switch | Hue Sync's *use audio for light effects* for video and games mode. Until you touch it, it mirrors what the Hue Sync app itself is set to. Flipping it hands the setting to Hue Ghost, which applies it at the start of the next sync |
| Sync offset | Number | How far the ghost runs ahead of the TV to cancel capture, bridge and lamp latency. Tune it from the couch: +0.25 s = lights later |
| Sync drift | Sensor | *(diagnostic)* The measured gap between the ghost and the TV, in seconds, which is the evidence for the offset above |

With movie mode on, everything else is automatic: press play on the TV and the lights follow, and when you stop, they stop. The PC's Hue Sync app needs *Allow public control* enabled, and the lounge room area selected (see the [Hue Ghost README](https://github.com/engabd11/HueGhost#readme)).

## Versions

Mode, intensity and the audio switch need **Hue Ghost 2.3.0** or newer. The brightness light and the per-source switches need **2.4.0**. On older versions the light still switches movie mode, and the level, the source switches and the remaining entities appear once Hue Ghost is upgraded. You choose the entertainment area per source in Hue Ghost, and Hue Ghost applies it in the Hue Sync app when a movie starts.

> **Upgrading from 1.59 or earlier.** The separate `switch.hue_ghost_movie_mode` is gone, because the Global sync light already did both halves of its job. It is removed from the registry on the first restart, so point any card or automation at the light instead. Entities you already have keep their old entity ids (Home Assistant keeps entity ids stable once created), and the new names show up as friendly names. To take the shorter ids as well, delete the Hue Ghost device under **Settings → Devices & services → Hue Synco** and reload the entry. It comes straight back with `light.hue_ghost_global_sync`, `sensor.hue_ghost_sync_status` and so on.

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
| `light`, `status`, `area`, `source`, `playing`, `intensity`, `mode`, `sources` | auto | Pin a specific entity, for example with two PCs |

Because it reads the sources from the device, a source added in the Hue Ghost app appears as a new tile on its own.
