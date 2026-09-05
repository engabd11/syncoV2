# Plan — port CAMusic's light-sync upgrades into syncoV2

CAMusic (Android, `com.engabd.sendpin.hue`) was ported from syncoV2 and then
evolved on its own. This PR brings the pieces of that evolution that matter for
music-sync-with-lights back into the HA integration. Out of scope (deliberately):
the rhythm game, ambience effects, colour schemes/album extraction (syncoV2's is
kept), the direct-path-only layer chain, and anything needing a phone tap
(`GestureTracker`, `FrameDelayQueue` — the HA path has its own timing calibrator).

Every item below was verified against both codebases (grep for the concept, not
a guessed name) before being listed.

## Verified gaps, in port order (one commit each)

### P1 — Hue-compliance core: colour matrix + wall-clock easing
- `hue/stream.py`: `rgb_to_xy` still uses the deprecated 2013 "Wide-RGB D65"
  matrix (0.649926/0.103455/0.197109 …). CAMusic moved to the current
  sRGB→XYZ D65 matrix (0.4124/0.3576/0.1805 …) per Philips' colour-conversion
  page; the two disagree most on saturated greens/blues. Swap + docstring.
- `effects/engine.py`: every per-frame easing coefficient (bri_attack/decay,
  colour_lerp, env rise/fall, presence, gate decay, flash decay, predrop
  rise/fall) is applied once per frame regardless of frame time. Port CAMusic's
  `frameAlpha`/`frameDecay` (exact conversion, identity at the nominal rate —
  TUNING_FPS = 50 here, the analysis/render rate, vs CAMusic's 60) and apply
  with the render dt the coordinator already passes.

### P2 — Brightness rate caps + non-bypassable effect-rate limiter
- Replace the rise-only per-frame `bri_slew` with CAMusic's per-second
  `briRiseRate`/`briFallRate` on every rung (fall previously unlimited — the
  "gentle dimming between beats" Philips guidance; brightness transitions
  slower than colour).
- Port `EffectRateLimiter` (per-channel 12.5 Hz transition ceiling, the
  Entertainment-API physical limit) into `effects/safety.py` and wire it in
  `coordinator._safe_send` so it binds even Extreme, which bypasses FieldSafety.
- dt-normalise FieldSafety's own EMA/engage/release/activity coefficients.

### P3 — Sustain bloom + loudness-aware melbank on every rung
- Port the tonal layer (tonalGain/tonalWidthMax/Soft/AttackS/ReleaseS/tonalDamp
  + chroma-stability gate): a held vocal/pad blooms room-wide instead of reading
  dark; damped by transients and mildly by rhythm confidence.
- Port `melPeakiness` (mean+peak blend in a lamp's melbank window) onto the main
  path with the `contrast` tunable reaching it; port mean-normalised absolute
  band-loudness weights (clamped 0.25–2.5) to the main render path, so the
  `loudness` tunable does something on all five rungs (Extreme keeps its raw form).

### P4 — Colour tilt + spatial coupling (cohesion)
- `colourTilt`: the colour field projects onto a tilted room axis (0.845/0.296/
  0.465) instead of x-rank only; two lamps at the same x, different heights
  stop being forced to the same colour.
- `spatialCoupling` + `couplingKernel` (row-stochastic Gaussian over lamp
  positions, σ from mean nearest-neighbour distance): pre-smooth the two
  continuous drives (melbank glow, attack pop at half strength) — never the
  output, so the role split, waves and flashes stay sharp.
- New `cohesion` tunable scaling it; presets carry CAMusic's tuned values.

### P5 — Live absolute melbank reference + Auto picker live fix
- `audio/analyzer.py`: keep a slow envelope of the raw pre-AGC melbin means and
  publish it as `frame.melbank_ref` for unmapped tracks (scan ref stays
  authoritative; empty until settled). CAMusic proved the "impossible" live
  estimate works: the AGC divides the *published* melbank, not the means a line
  before it.
- `effects/modes.py::AutoIntensityPicker`: sample the live attack term on frames
  that actually carry an onset (energy-weighted busy/bass too), matching the
  offline profile's energy-weighted mean. Fixes Auto sitting on the lowest
  enabled rung: the old estimator averaged onsetWidth over every frame —
  near zero between transients, i.e. most frames — dragging the heaviest
  character weight to ~0.

### P6 — Behavioural parity (small, independent)
- Warm-up highlight floor 0.3 (a beat ranked before the window has context must
  still earn its flash).
- Idle show records `emit_b` (slew memory) — resuming from idle no longer
  clamps the first music frame against a pre-pause brightness and flashes.
- `render_idle*` resets the pre-drop state machine.
- Bar-weight tables per metre + `beats_per_bar` on `BeatGrid` (engine machinery;
  the map still measures 4/4 until metre detection is ported — noted).
- Locked grids keep the reactive kick via max(scheduled, reactive) as CAMusic
  does — included only if `test_conductor` agrees; otherwise documented as a
  deliberate keep (syncoV2's `_visible_event` already takes the max on strength).

## Tests
Port/extend per part: matrix invariance (existing gamut/slew tests must stay
green), `frameAlpha` identity at nominal rate + wall-time tracking, per-second
rate caps + limiter (reuse CAMusic's FieldSafetyTest/EffectRateLimiter cases),
bloom (held vocal brightens, percussion doesn't), coupling (spread shrinks,
peak doesn't shift), tilt (flat room byte-identical), live ref (uniform ==
none), picker (onset-sampled attack), warm-up floor, idle slew.

`pytest tests/` must pass at every commit; CI (hassfest/hacs/pytest) is green
on main and runs on the PR.
