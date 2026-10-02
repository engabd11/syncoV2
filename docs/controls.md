# Controls

Each entertainment area gets these entities:

| Entity | Type | Description |
|---|---|---|
| Sync | Switch | Starts and stops the light show for this area |
| Intensity | Select | Choreography intensity (see [Intensity modes](#intensity-modes)) |
| Effect | Select | Rendering style (Music, Movies, Fireworks) |
| Colour | Select | Colour palette source |
| Brightness | Number | Master brightness ceiling (5 to 100%) |
| Timing offset | Number | Manual sync trim in milliseconds (-500 to +500), see [Timing and Auto timing](#timing-and-auto-timing) |
| Advanced controls | Switch | Reveal and apply a set of live **tunable knobs** under the intensity |

While an area is syncing, its switch also exposes now-playing, album-colour, tempo and audio-source attributes. That is the state the card runs on, and it is available to your own automations too.

## Advanced controls

Turn on **Advanced controls** (from the switch entity or the toggle on the card) to reveal a row of live sliders under the intensity picker: **Reactivity**, **Glow**, **Movement**, **Contrast**, **Colour speed** and **Loudness**. Each is a 0 to 200% multiplier on the active mode's behaviour (100% is the mode as designed), applied **live during the song** so you can dial the room in by ear. They scale whatever the current mode uses and quietly skip anything it leaves out (for example, Movement only spins Extreme's spatial map). Turning Advanced back off restores the mode's defaults, and *Reset to 100%* clears the sliders.

## Timing and Auto timing

Two different things line the lights up with the music, and they solve two different problems.

**Auto timing** keeps the lights locked to the *player*. It follows seeks, track skips and playback stutters, cancels any drift between the player's clock and Home Assistant's, and on a live Music Assistant tap it also measures the decoder's own startup slippage each song. On a Sendspin player it does this from Sendspin's synchronised microsecond clock, so the playhead is exact rather than estimated. On everything else it anchors on the player's reported position and tracks it with a drift-corrected rate. Corrections are eased in rather than jumped, so playback stays smooth.

**The ± timing offset** is the delay of your *room*: how long your speakers, amplifier and globes take to produce sound and light. Only your ears can judge that delay, so Auto leaves it to you, and the steppers stay live while Auto is on. Set it once and leave it.

Auto is applied **on top of** your trim, as an addition to it. The card shows your trim as the main number and Auto's contribution as a small chip beside it:

| Chip | Meaning |
|---|---|
| `A ✓` | Locked to the player: this source needs only tracking |
| `A +120` | Auto is adding 120 ms on top of your trim (a live tap's startup slippage) |
| `A ⟳` | Measuring this song |
| `A` and a dash | This source reports its timing exactly, so the card leaves it alone |

While an area is syncing, the switch exposes `timing_applied_ms` (the delay actually in effect) and `clock_source` (`sendspin` or `ha-state`), and the card's WebSocket feed carries the full breakdown: clock confidence, estimated rate, residual error and the base, trim and auto split. Use them to see exactly what it is doing.

## Library device

Once per installation, a **Hue Synco Library** device manages the analysis cache:

| Entity | Type | Description |
|---|---|---|
| Analyse library | Button | Kicks off (or resumes) the background library pre-analysis, skipping already-cached tracks |
| Reanalyse library | Button | Wipes the cache and re-analyses **every** track from scratch, the way to upgrade the whole library to a newer analysis format |
| Delete library cache | Button | Removes every cached track map from disk (frees the space). Tracks re-analyse on their next play. |
| Library analysis | Sensor | Live progress, with failure details in its attributes |
| Library cache size | Sensor | Total size on disk of the analysed track maps (MB) |
| Library cached songs | Sensor | How many songs are currently cached |

## Intensity modes

| Mode | Flash limiter | Character |
|---|---|---|
| **Auto** | Follows the pick | Scores how hard each song goes on an absolute scale, gives it the band of the ladder it has earned, and moves within that band on the song's own arc, rescaled onto the rungs you enable (Subtle, Medium and High by default). Chill music stays low, and only genuinely heavy tracks reach the top of your range |
| **Subtle** | Strict | Gentle spatial gradient, soft colour drift, small beat steps, for very soft music (lofi, ambient) |
| **Medium** | Strict | Visible dimming, soft flashes on stronger beats, wide colour spread |
| **High** | Strict | Per-instrument spatial split (bass, guitar, vocal), dynamically assigned to the instruments actually playing. The everyday rung for soft and dance music alike |
| **Intense** | Relaxed | Unified club look with a fast but smooth dim to bright swing on the beat, colour shifting each hit, and a soft glow kept in the gaps. Scheduled beats are gated by real onset flux, so phantom grid beats through a tail or breakdown stay quiet |
| **Extreme** | Bypassed | A **direct "the song is a graph" renderer**: the beat grid is set aside and every flash comes from a real transient. Each lamp reflects its own slice of the spectrum, with a glow for its band's loudness and a flash on a fresh transient, weighted by the band's **absolute** loudness (a kick outshines a faint cymbal) and its stereo side. Instruments separate across the room and the map **rotates**, so every lamp takes turns on every instrument. A **true dark room**: quiet parts go black, and every hit brightens out of the dark |

See the photosensitivity warning in the [README](../README.md) for what Strict and Relaxed mean.
