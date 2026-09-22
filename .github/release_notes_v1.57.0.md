# Hue Synco v1.57.0 — Movie mode (Hue Ghost)

Adds a Home Assistant-controlled **movie mode**, switching a [hue-ghost](https://github.com/engabd11/HueGhost) PC client on and off so the same entertainment area that does music sync can also follow whatever's playing on the TV — no Hue Sync Box required.

## New

- **Hue Ghost — Movie mode device** — optional host/port/token for a hue-ghost PC client, configured via **Configure → Hue Ghost PC host / port / token**
- **Movie mode switch** — turns hue-ghost on/off; turning it on stops every active music-sync area first, since a bridge only allows one streamer per entertainment area
- **Movie mode state sensor** — `offline` / `idle` / `ghosting` / `syncing`, with now-playing, measured ghost-vs-TV drift, and the Hue Sync app state as attributes
- **Movie intensity select** — Hue Sync's video intensity (subtle / moderate / high / extreme), applied when a movie starts
- **Movie sync offset number** — tunes how far the ghost runs ahead of the TV to cancel capture → bridge → lamp latency
- **Clean hand-over both ways** — starting a music-sync area asks movie mode to let go first (best effort, before the DTLS stream starts)

## Requirements

- Home Assistant 2024.12.0 or later
- Philips Hue bridge with Entertainment API support
- Music Assistant integration installed and configured
- `numpy` (auto-installed by HACS)
- Optional: a [hue-ghost](https://github.com/engabd11/HueGhost) PC client for movie mode

## Upgrading

Update via HACS, then restart Home Assistant. Movie mode is opt-in — existing music-sync setups are unaffected until you add a Hue Ghost host in the integration options.
