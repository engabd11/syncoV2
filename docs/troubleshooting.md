# Troubleshooting

## Fixes

- **Card shows "Custom element doesn't exist".** Usually a stale cached app shell, so reload the page once. The integration registers the card as a Lovelace resource automatically. If you removed that resource by hand, re-add `/hue_music_sync/hue-music-sync-card.js` under **Settings → Dashboards → Resources**, or reload the integration.
- **Lights react late or early.** Use the card's timing-offset stepper (±ms) to land the flashes exactly on the audible beat in your room. Leave **Auto** on as well, because the two are complementary (see [Timing and Auto timing](controls.md#timing-and-auto-timing)).
- **The "Metadata only" pill stays amber.** Hue Synco is still waiting for a stream or a track map. Run **Analyse library** once, or configure the OpenSubsonic options so tracks can be fetched for analysis.
- **The first play of a new track reacts generically.** Full offline analysis takes about 10 s on slower hardware, and the show upgrades mid-song when it lands. `prewarm_library` removes the wait entirely.
- **Position-coarse players (for example Sonos).** A player that reports its position rarely and roughly gives the playback clock less to work with. It still tracks correctly, because the clock free-runs at a drift-corrected rate between reports, but a large glitch takes a little longer to confirm.
- **Bridge unreachable after a factory reset.** A factory reset gives the bridge a new certificate that differs from the paired bridge id (by design). Remove and re-add the integration.
- **Stopping sync in the Hue app.** Since 1.54.0, Hue Synco subscribes to the bridge's event stream and checks who owns the area before reconnecting, so stopping from the Hue app turns the Home Assistant switch off instead of taking the area back.

## Known limits

- **Hue Bridge v2.** The round v1 bridge predates Entertainment streaming.
- **One area streaming at a time per bridge.** A bridge has a single DTLS channel, and multiple bridges each get their own entry.
- **Cache upgrades after updates.** A newer analysis format is backward-compatible: existing maps keep working, and newly played or analysed tracks use it. To apply a format upgrade across your whole library at once, press **Reanalyse library** (`prewarm_library` with `force: true`).
