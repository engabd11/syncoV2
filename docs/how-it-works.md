# How it works

From the audio on a media player to light on the bridge:

```
Audio source ladder (per followed player, best available wins):
  MA/Sendspin stream decode → OpenSubsonic library decode → offline track map
  → metadata glow   (Snapcast tap sits above these when configured: legacy)
        ↓
Real-time analysis (5-band FFT, 16-bin melbank, SuperFlux onsets, tempo,
absolute-loudness salience + onset broadbandness for event selection)
        ↓
Offline track map (beat grid, downbeats, section boundaries: analysed once,
then cached to disk so the same track reacts instantly next time)
        ↓
Album art → CIELAB colour palette extraction
        ↓
Effect engine (instrument roles, spatial waves, brightness envelopes, palette sampling)
        ↓
Eye-safety stage (flash limiter: strict or relaxed per mode, brightness floor,
red guard, gamut clamp, colour slew)
        ↓
HueStream encoder (RGB → xy chromaticity + brightness, Gamut C clamping)
        ↓
Pure-Python DTLS 1.2 (PSK mutual auth, AES-128-GCM) → Hue Bridge
```

The DTLS transport is implemented in pure Python and is self-contained, covering exactly what the bridge needs: PSK handshake with **enforced server verification**, AES-128-GCM record encryption, keepalives, and a graceful close so the bridge frees the session immediately when sync stops.

## Rates

- Frames are sent to the bridge at 60 a second, the rate the Hue Entertainment spec recommends.
- The bridge relays to the lamps at 25 Hz or less over Zigbee. Hue Synco keeps the visible effect rate at 12.5 Hz per channel or lower in every mode.
- The analysis runs at about 50 frames a second, and the card's live feed updates at about 20 Hz.

## The card feed

The card talks to the integration over Home Assistant's WebSocket API (`hue_music_sync/subscribe` for the live feed, `/players` for the picker, `/tap` and `/drum` for the drum pad), so everything on it (bars, room mirror, timeline) reflects the actual session as live data rather than a simulation.
