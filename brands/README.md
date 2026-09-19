# Brand assets

Icon for the Hue Music Sync integration, in the layout the
[home-assistant/brands](https://github.com/home-assistant/brands) repo expects.

```
brands/custom_integrations/hue_music_sync/
├── icon.png      256×256, RGBA
└── icon@2x.png   512×512, RGBA
```

> **HACS does not read this folder.** For a custom integration, HACS (and the
> Home Assistant UI) fetch the icon from `https://brands.home-assistant.io`,
> keyed by the `domain` in `manifest.json` (`hue_music_sync`). Until the brands
> repo has that domain, HACS shows the default puzzle-piece icon. These files are
> kept here as the source of truth, ready to submit.
>
> Since September 2025 the HACS validation action also accepts in-repo brand
> assets at `custom_components/hue_music_sync/brand/` and fails the `brands`
> check otherwise (the `ignore: brands` workflow option no longer covers it), so
> the same two files are duplicated there. Keep both copies identical.

## Make it show up in HACS

1. Fork [home-assistant/brands](https://github.com/home-assistant/brands).
2. Copy `custom_integrations/hue_music_sync/` from here into the fork at the same
   path.
3. Open a PR. Once merged, the icon appears in HACS and on the integration's
   device pages (after the CDN refreshes).

Brand requirements (already met by these files): square PNG, transparent
background, `icon.png` exactly 256×256 and `icon@2x.png` exactly 512×512.

## Regenerate

```bash
python scripts/make_icon.py
```
