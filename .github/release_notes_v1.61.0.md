## Hue Ghost: Home Assistant mirrors the PC reliably

- **Sync status has a `disabled` state.** Global sync off now reads `disabled`, and `idle` only means "on, nothing playing". An automation can tell the two apart from the sensor alone.
- **A slow answer from the PC no longer flips Global sync off.** hue-ghost can be busy for a few seconds (launching the ghost, restarting Hue Sync). A single failed poll used to turn the light off and back on, an edge automations fired on. The last state is now kept until three polls in a row fail.
- **Unreachable reads as *unknown*, not *off*.** The light stays usable so it can be switched on when the PC is back.
- **Commands show up immediately.** On/off and every `/set` apply the daemon's answer to the entities straight away, so an automation that switches Global sync and then checks it sees the new value.
- **Longer timeouts.** 10 s for status and 20 s for commands, up from 5 s. Switching off waits for Hue Sync to confirm the stop, which used to raise an error for an off that had worked.
- **`syncing` attributes** (light and Sync area) now come from the Hue Sync app itself, so they're true whoever started the sync.
- Polls every 3 s (was 5 s).

Pair with **hue-ghost 2.10.2**, which stops a second copy of the app from starting. Two copies were the root cause of "the app says on and syncing, HA says off and idle": HA talked to one copy, the window belonged to the other, and each copy's shutdown closed the other's ghost, which switched sync off by itself.
