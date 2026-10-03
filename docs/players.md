# Players and libraries

Hue Synco follows a media player in Home Assistant and picks the best audio source it can get for it. Music Assistant players are preferred.

## Player support

**Any Music Assistant player works out of the box.** The integration picks the best audio strategy automatically and upgrades live when a better one becomes available:

| Player type | Audio source |
|---|---|
| **Sendspin** | Position-locked decoding of the track's stream: real live audio, full beat accuracy, and an exact playhead from Sendspin's own synchronised clock |
| **Squeezelite / Slimproto** | Position-locked stream decoding (re-syncs on drift) |
| **AirPlay, Chromecast, Sonos, DLNA, ESPHome, groups** | Pre-analysed track map for full beat accuracy |
| **Any player at all** | Metadata fallback: a gentle animation, upgraded to a real source automatically the moment one becomes tappable |
| Snapcast-backed players *(experimental and legacy)* | Real-time stream tap with automatic buffer alignment, see [legacy sources](#experimental-and-legacy-audio-sources) |

The followed player can be **pinned per area** (from the card's player dropdown or the `set_options` service) or left on auto.

## Navidrome and OpenSubsonic libraries

Point Hue Synco at your **Navidrome or any OpenSubsonic** server (library URL and login in the integration options) and it fetches and analyses your library directly.

When Music Assistant offers a player's stream for tapping, Hue Synco uses it. Otherwise it matches the track to your library, decodes it from the server and analyses it into a cached track map. That is how a **Sendspin** player streaming from OpenSubsonic reacts with full beat accuracy.

Run **Analyse library** once to pre-warm the whole catalogue so every song reacts from its first beat (see [Controls](controls.md#library-device) and [Services and options](services-and-options.md)).

## Experimental and legacy audio sources

Hue Synco began life around Snapcast and Squeezelite before focusing on Music Assistant players. These paths still work and stay available for setups that use them. They are optional and see lighter testing:

- **Snapcast tap**: when a Snapcast server host is configured *and* the followed Music Assistant player is Snapcast-backed, the live stream is tapped directly with automatic buffer alignment. It is the most latency-accurate source, and you run snapserver yourself.
- **OpenSubsonic / Navidrome**: when a player's stream URL is out of reach (for example Sendspin with an OpenSubsonic provider), the integration builds the standard `/rest/stream` request itself from your library URL and login and decodes that. The library pre-analysis uses it too.
  - A bare address defaults to **https://**. For a plain HTTP server on your LAN, write `http://` explicitly.
