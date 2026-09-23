# Hue Synco v1.59.0 — Area brightness, and a switch per source

Movie mode gets a **light** you can put in a scene, and every source the PC can follow becomes its own switch — including the new ones in [Hue Ghost 2.4.0](https://github.com/engabd11/HueGhost/releases): apps playing on the PC itself, and music from Jellyfin.

## New

- **Movie mode lights** (`light`) — the entertainment area as a real light entity: on/off is movie mode, and the brightness slider is the level Hue Sync runs the area at. Usable in scenes, in automations and by voice, unlike a settings number. Hue Sync's protocol has no absolute brightness command, only a signed step; Hue Ghost turns a level into that step against the level the app reports, so a plain 0–255 slider works.
- **A switch per source** — one switch for every Jellyfin client and PC app Hue Ghost is set up to follow. Turning one off makes Hue Ghost ignore it without deleting it. Attributes say which area it drives, whether it is a TV client or an app on the PC, any problem stopping it working, and — separately from whether it is *followed* — whether it is the one **actually driving the lights right now** (`active`), so an automation can tell those two apart.
- Sources added or removed in the Hue Ghost app appear and disappear here without reloading the integration.

## Requirements

- Home Assistant 2024.12.0 or later
- Philips Hue bridge with Entertainment API support
- Music Assistant integration installed and configured
- `numpy` (auto-installed by HACS)
- For movie mode: a [Hue Ghost](https://github.com/engabd11/HueGhost) PC client. **The brightness light and the source switches need Hue Ghost 2.4.0 or later** — against an older PC the light still switches movie mode but reports no level, and no source switches appear at all.

## Upgrading

Update via HACS, then restart Home Assistant. Nothing changes for existing setups: the new entities only appear when a Hue Ghost host is configured, and music sync is untouched.
