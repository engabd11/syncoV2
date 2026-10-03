# Development

## Code layout

```
custom_components/hue_music_sync/
  audio/      sources (MA stream, track map, snapcast, metadata), analyzer, tempo, structure
  color/      album-art & song palettes
  effects/    engine, modes, spatial, fireworks, safety limiter
  hue/        CLIP v2 client, pure-Python DTLS 1.2, HueStream encoder
  frontend/   the bundled dashboard card (no build step)
tests/        pure DSP/colour/encoder unit tests (no HA install needed): pytest tests/
tests_ha/     config-flow tests on the HA harness (run in CI)
scripts/      developer spikes & analysis tools, not part of the integration
```

## Tests

Run the unit tests with `pip install pytest numpy aiohttp cryptography`, then `pytest tests/`.

CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation and the unit tests on every push and pull request to `main`.

## More

- [how-it-works.md](how-it-works.md): the pipeline from audio to bridge
- [plan/camusic-light-sync-upgrade.md](plan/camusic-light-sync-upgrade.md): the plan for porting CAMusic's light-sync upgrades into this integration
