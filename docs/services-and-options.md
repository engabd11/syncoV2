# Services and options

## Services

| Service | Description |
|---|---|
| `hue_music_sync.activate` | Start sync for one or more areas; optionally set mode, effect, colour, brightness and the followed player |
| `hue_music_sync.deactivate` | Stop sync |
| `hue_music_sync.set_options` | Change any setting live, mid-session, including pinning or clearing the followed player |
| `hue_music_sync.prewarm_library` | Analyse your whole Music Assistant or OpenSubsonic library in the background and cache it to disk, so **every** track reacts with full beat accuracy the first time it plays. `retry_failed: true` clears recorded failures (and ambient-only maps) so they analyse again, and `force: true` wipes the whole cache first and re-analyses everything (upgrade the library to a newer map format) |
| `hue_music_sync.clear_library_cache` | Delete the whole on-disk track-map cache (every analysed song), freeing the disk space. Tracks re-analyse on their next play |
| `hue_music_sync.analyze_track` | Diagnose **one** song right away and post the verdict as a notification: tier, tempo, confidence, and exactly why a beat grid was rejected. Takes a stream `url`, an `artist` and `title` library lookup, or a `media_player` that is currently playing |

Pre-analysing the library (or pressing the **Analyse library** button) is the way to make a brand-new track react instantly. It runs gently, one track at a time and yielding to live playback, and it is resumable **and incremental**: re-running only analyses what is new, so it is also how newly added Navidrome or library tracks get picked up.

## Library analysis, failures and re-analysis

Every analysed track lands in one of three tiers:

- **Full**: a trustworthy beat grid was found, giving scheduled, anticipatory beat playback (the best show).
- **Ambient**: the audio decoded fine but the beat schedule was uncertain (freely timed, rubato or beatless material). The lights still get everything else: per-frame energy and spectrum, sections, song colours and the *detected* onsets, which the live beat tracker locks onto within a few seconds. Every decodable track is lit from its audio, and the metadata animation is kept for tracks whose audio is out of reach.
- **Failed**: the fetch or decode failed (a URL, login or network issue). These are recorded persistently with the reason.

Where to look:

- The **Library analysis** sensor: `failed` and `newly_ambient` counts, a capped `failed_tracks` list (`Artist - Title` and the reason), and `pending` (tracks enumerated and waiting for analysis).
- The full uncapped report is written to `config/hue_music_sync/trackmaps/analysis_report.json` after each sweep.
- `hue_music_sync.analyze_track` re-analyses a single song on demand and explains its verdict (beats-on-peaks contrast, interval spread against the local tempo, coverage and tempo stability).

The tempo analysis follows **drifting and changing tempo** (a live drummer, a 100 to 140 BPM switch) through a windowed tempogram with Viterbi tempo-path decoding, so dynamic, human-played music gets a full-tier grid. After updating to a release that improves the analysis, run `prewarm_library` with `retry_failed: true` once so previously failed or ambient tracks get re-scored. To upgrade **every** cached map to a new analysis format, press **Reanalyse library** (`prewarm_library` with `force: true`).

## Options

**Settings → Devices & Services → Hue Synco → Configure**:

| Option | Description |
|---|---|
| Enabled entertainment areas | Which areas get entities |
| Restore lights on stop | Snapshot and restore light state when sync stops |
| Snapcast server host | *(experimental and legacy)* real-time audio tap for Snapcast-backed players |
| Sendspin server host | *(optional)* leave blank to find it from Music Assistant. Sendspin's synchronised clock gives the lights an exact playhead instead of an estimated one |
| OpenSubsonic URL / credentials | *(optional)* direct library-track streaming and analysis via Navidrome or any OpenSubsonic server |
| Music library backend | Music Assistant, or Navidrome/OpenSubsonic direct (needs the URL and login above, and keeps browsing going when Music Assistant is unavailable) |
| Hue Ghost PC host / port / token | *(optional)* enables [Movie mode](movie-mode.md) via a Hue Ghost client on the LAN |
