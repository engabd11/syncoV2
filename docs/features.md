# Features

What Hue Synco does and how it decides what to light. For the pipeline behind it, see [how-it-works.md](how-it-works.md).

## What it does

Hue Synco follows whatever is playing on your chosen media player and translates the audio into a synchronised light show across a Hue entertainment area.

Every track is analysed once in the background: beats are located precisely, and downbeats and section boundaries are found. During playback those events are **scheduled** ahead of time, so the choreography lands exactly on the beat rather than chasing it. A continuous spectral layer (a 16-band melbank spread spatially across the room) keeps every lamp alive between beats.

What reacts matters as much as when. Reactions are **proportional to the sound's real loudness** (a quiet pluck gives a small dim pulse, the drop slams the room) and are keyed to **instruments rather than vocals**: sung melodies and sustained tones are filtered out of the beat streams so the lights follow the music rather than the singing.

You choose which player drives the lights. Pick any player from the card, or let the integration auto-follow whatever is playing (Music Assistant players preferred).

## Audio analysis

- **Full-track beat tracking**: a dynamic-programming beat tracker runs offline, so beats are pre-located instead of guessed in real time
- **SuperFlux onset detection**: spectral flux on log-compressed magnitudes with vibrato immunity; separate bass/kick, mid/guitar and broadband streams keep hi-hats from triggering false beats
- **Salience-proportional reactions**: every flash, wave and scheduled pulse scales with the sound's *absolute* loudness relative to the track, so quiet sounds stay small and a locked beat grid eases back through breakdowns
- **Vocal rejection**: onsets are classified by how broadband their spectral flux is. Drums splash across the spectrum, while sung vowels stay narrow and are muted (with a soft knee, per intensity mode)
- **5-band frequency decomposition** with per-band automatic gain control
- **16-bin melbank**: a continuous, exponentially smoothed spectrum spread left to right across the room
- **Song structure detection**: section boundaries are found and ranked by energy. Brightness swells on drops, desaturates during builds and breathes during breakdowns
- **Library pre-analysis**: a background sweep analyses your whole library ahead of time (resumable, survives restarts, one track at a time, always yielding to live playback) with a progress sensor and failure reporting

## Choreography

- **6 intensity modes** (Auto, Subtle, Medium, High, Intense and Extreme) sharing one unified renderer. The mode also sets how *picky* beat selection is: the mode **is** the sensitivity.
- **Auto intensity** picks a rung for you from what the music *is*, beyond how loud it happens to be. Every song is first scored on an **absolute** character scale (tempo, how dense the beat is, how percussive the attacks are, its low-end weight and how relentlessly it sits near its own peak), and that score decides which **band of the ladder the song has earned**. A lofi track tops out around Medium however big its own chorus gets, and only a genuinely heavy track reaches the top of the range you enable. The song's own verse, chorus and drop arc then moves it within that band, so it still climbs on a drop and eases back in the quiet parts. A rung depends on character rather than loudness, because the analysis normalises every track to its own peak: a chill chorus and a festival drop both read as loud.
- **The rungs you enable are a palette rather than a forced range.** A checklist on the card sets which rungs Auto may choose from (Subtle, Medium and High by default), and the ladder is **rescaled** onto your selection rather than clipped. Pick *Subtle to High* and a banger reaches High while a chill track still only reaches Medium. Making High the lowest enabled rung makes it the floor. Each song uses only the rungs it has earned from your selection, and an energetic track stays off Subtle even when its verse is its own quietest bar. Rung widths are deliberately uneven (High and Intense carry most music, Subtle is for genuinely soft material, Extreme is a narrow spike), so the balance holds whether you enable three rungs or five. **Whatever you enable, the room moves**: if a song's earned band lands inside a single one of your rungs, the band is widened just enough to reach the next boundary, so its peaks still lift a rung and its breakdowns still drop one.
- **Measured per song, and free on a pre-warmed library** where the analysis is already cached. The rung timeline is **lag-free**: because the whole song is known ahead of time, a switch is scheduled to land right as the section changes (a drop reaches its rung on the beat itself), and a new song re-evaluates from its own opening so each track starts fresh. A flat, constant-loudness track stays put, and the song's mood (bass-heavy club or mellow and bright) nudges where in its band each moment sits. On a live tap before the analysis lands, Auto estimates the same character over the first 20 seconds or so, starting mid-ladder so an unknown track opens on a middle rung. Small one-rung nudges dwell so the pick stays calm, and only a real drop or breakdown moves fast.
- **Instrument role assignment**: lights are split into bass, guitar and vocal roles, spread evenly around the room and re-dealt every few bars. It scales cleanly from 1 to 10 lights.
- **3D spatial waves**: beat wavefronts sweep the room using the actual lamp positions from the entertainment area, with lows to one side, highs to the other and treble to the higher lamps
- **Beat highlight selection**: brightness pops on beats that stand out against the recent 24-beat window

## Colour

There are 19 colour options: three colour sources and 16 themes.

- **Album colours**: dominant colours pulled from cover art in perceptual CIELAB space. Muted artwork stays muted, and colours are re-extracted on every track change.
- **Album colours v2**: the same extraction, with each colour carrying its share of the cover, so the lights spend time on it in proportion. A cover that is 90% green and 10% red renders a green room that shifts to red in moments.
- **Song colours**: a palette derived from the track's pitch content, shifting with each section.
- **16 preset themes**: Sunset, Ocean, Forest, Lavender, Ember, Aurora, Rainbow, Tropical twilight, Savanna sunset, Spring blossom, Honolulu, Galaxy, Neon, Peacock, Citrus and Rose gold.

## Effects

- **Music**: full beat and frequency choreography (default)
- **Movies**: calm and unobtrusive. Brightness follows soundtrack energy with a warm cinematic drift and a steady glow.
- **Fireworks**: bursts ignite on big beats with a rapid fade-out
