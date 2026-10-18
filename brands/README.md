# Brand assets (PLACEHOLDERS — replace before publishing)

These are **generic placeholder** images so the layout/sizing is correct. Replace them with real
Govee-BLE-Local artwork before any submission to Home Assistant. They were produced by
`make_placeholders.py` (pure stdlib, no Pillow); regenerate with:

```sh
python3 brands/make_placeholders.py
```

The script writes every image twice: into `custom_components/govee_ble_local/brand/` (the copy
that ships) and into this folder. Real artwork must go into both as well;
`tests/test_brand_assets.py` fails when the two copies differ.

## Files & sizes (match the home-assistant/brands spec)

| File | Size | Notes |
| --- | --- | --- |
| `icon.png` | **256×256** | Square, transparent background, trimmed to the mark |
| `icon@2x.png` | **512×512** | hDPI icon |
| `logo.png` | **≤256** longest side (here 256×128) | May be rectangular (wordmark) |
| `logo@2x.png` | **≤512** longest side (here 512×256) | hDPI logo |

Requirements when you replace them: PNG with transparency, trimmed (minimal padding), and
optimized (e.g. `pngquant`/`optipng`). Icons must be square.

## Where they go
Home Assistant 2026.3 and later serves the images bundled with the integration, from
`custom_components/govee_ble_local/brand/`, so they arrive with every install (HACS included).
Older supported releases (2025.10 to 2026.2) only look them up on the
**[home-assistant/brands](https://github.com/home-assistant/brands)** CDN, which has none for this
integration, so they show Home Assistant's generic placeholder. That repo now treats its
`custom_integrations` folder as legacy; a PR there would add the files from `brand/` as:

```
custom_integrations/govee_ble_local/icon.png
custom_integrations/govee_ble_local/icon@2x.png
custom_integrations/govee_ble_local/logo.png
custom_integrations/govee_ble_local/logo@2x.png
```

This folder is a staging copy that lives next to the generator. `quality_scale.yaml` marks the
`brands` rule done for the bundled `brand/` folder; the artwork is still a placeholder (no Govee
trademarks) and has to be replaced before any core submission.
