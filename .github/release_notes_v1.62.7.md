## Saved radio stations sync to live audio

Saved Music Assistant radio stations now drive the lights from the real stream, where many of them used to sit on **Metadata only** (#14).

- **TuneIn stations get live audio.** Music Assistant keeps a saved TuneIn station's stream on its provider entry, and Hue Synco now reads it from there.
- **Radio Browser stations get live audio too.** Music Assistant only shares a Radio Browser station's ID, so Hue Synco asks the public Radio Browser API (`all.api.radio-browser.info`) for that station's stream, by ID only. It does this only when nothing else gives a stream. Answers are cached for 30 minutes, and a station that is already playing keeps its stream if the API is slow or down.
- **Every stream is still checked.** A station plays through the light show only once ffmpeg has decoded real audio from it; anything else falls back to the metadata glow as before.

## Steadier player switching

- **Switching the media player during sync no longer logs an error.** The sync loop could briefly run without an audio source after a switch and logged `'NoneType' object has no attribute 'track_id'` before recovering. It now moves straight to the new player.

Includes everything in 1.62.6. Update through HACS and restart Home Assistant.
