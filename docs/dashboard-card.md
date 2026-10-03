# Dashboard card

The **Hue Synco Card** ships inside the integration and registers itself as a dashboard resource automatically, so it installs with the integration and appears in the card picker as *Hue Synco Card*. Updates are picked up automatically: the resource URL carries a content hash, so every update loads fresh. Dashboards in YAML mode add the resource `/hue_music_sync/hue-music-sync-card.js` by hand.

## The hero

An immersive now-playing header themed by the music itself. The blurred album art bleeds into the backdrop and the extracted album colours drive every accent:

- **Audio-source pill**: what is driving the lights right now (`Live audio`, `Live (snapcast)`, `Track map` or `Metadata only` in amber), so a stalled tap shows up at a glance
- **Power toggle**, **album art with beat gloss**, **marquee title and artist**, **live brightness readout** and large centred **transport controls** that drive the followed player directly
- **Song-structure timeline**: the track's energy silhouette with section boundaries and a moving playhead. The next section pulses as a drop approaches.

## The body

- **Visualiser bars**: a real feed of the analysis output at about 20 Hz, delayed through the same timing buffer as the lights, so it renders exactly what the room is reacting to
- **Room mirror**: every lamp at its real position, glowing in the exact colour being streamed to it, with rings marking its instrument role
- **Area and Player dropdowns**: side-by-side titled selectors. One card controls several areas, and the lights can be pinned to any player (or Auto). Menus open as floating popovers that scroll when the list is long.
- **Library** and **Speaker group** buttons: browse and search your library and play from the card (Music Assistant or Navidrome/OpenSubsonic), and group speakers for synchronised playback
- **Intensity selector** with live micro-animation previews, **colour palette dots**, **brightness slider** and a **timing-offset stepper** (±ms fine trim) with an **Auto** toggle that calibrates the per-song startup delay for you. It locks a value early in each track and falls back to the manual trim when off.
- **Beat Pads**: a full-page overlay of three tall Low, Mid and High tap columns, each driving a third of the room. While it is open the automatic beats pause so *your* taps flash the lights, and it releases automatically when closed.

## Behaviour

- **Idle beauty**: while paused, the card (and the room) drifts slowly through the palette instead of freezing
- **Ambient idle show**: when the music is paused or nothing is queued, the room blossoms into a slow, wandering glow. Colours drift through the palette while two gentle waves cross the room out of phase, so the light seems to move on its own (dim and steady, a lava lamp rather than a strobe). It emerges after a few seconds of real idle and fades its movement in, so the brief gap between songs keeps the normal show running.
- Respects `prefers-reduced-motion`, and pauses all animation when scrolled off-screen (wall tablets keep dashboards open around the clock)
- **Demo mode**: add the card with an empty config and it renders a self-running demo, so you can style your dashboard before wiring entities

## Tablet layout

A landscape-optimised sibling, the **Hue Synco Card (Tablet)**, ships alongside the mobile card in the same soft Hue-navy theme, built for wall tablets and dashboards viewed sideways. Search for *Hue Synco Card (Tablet)* in the card picker. It shares all of the mobile card's real-data wiring (live analysis feed, album-colour extraction, player picker, Beat Pads) in a two-column arrangement:

- **Left column**: a large centred album cover with a beat-reactive halo, marquee title and artist, centred transport, a **waveform scrubber** with time readout and a **live frequency map** (one glowing dot per lamp, laid out low to high across the room)
- **Right column**: the Area and Player dropdowns, an **intensity picker** with a live per-mode equaliser, the Effect segmented control, colour palette dots, and the brightness slider and timing stepper (with the Beat Pads button)

## Card configuration

The card picker pre-fills a working template. Full form:

```yaml
type: custom:hue-music-sync-card
areas:
  - name: Lounge room
    switch: switch.music_sync_lounge_room
    intensity: select.music_sync_lounge_room_intensity
    effect: select.music_sync_lounge_room_effect
    colour: select.music_sync_lounge_room_colour
    brightness: number.music_sync_lounge_room_brightness
    timing: number.music_sync_lounge_room_timing_offset
    media_player: media_player.lounge_room   # optional; the picker can change it live
  # ...more areas
```

A single flat area (`switch:`, `intensity:` and so on at the top level) also works. Every key is optional: the card renders whatever you give it and shows demo mode for an empty config.

The tablet card takes the **same** configuration, so just change the type:

```yaml
type: custom:hue-music-sync-card-tablet
areas:
  - name: Lounge room
    switch: switch.music_sync_lounge_room
    # ...same keys as above
```

A third card ships in the same bundle for movie mode: the **Hue Ghost Card (Movie mode)**, in the Hue Ghost app's warm gold rather than Hue navy. It configures itself, see [Movie mode](movie-mode.md#the-hue-ghost-card).
