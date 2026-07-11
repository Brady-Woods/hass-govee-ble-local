# Brand assets (PLACEHOLDERS — replace before publishing)

These are **generic placeholder** images so the layout/sizing is correct. Replace them with real
Govee-BLE-Local artwork before submitting to Home Assistant. They were produced by
`make_placeholders.py` (pure stdlib, no Pillow); regenerate with:

```sh
python3 brands/make_placeholders.py
```

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
Home Assistant loads brand images from the **[home-assistant/brands](https://github.com/home-assistant/brands)**
repo, not from here. For a custom integration, submit them under:

```
custom_integrations/govee_ble_local/icon.png
custom_integrations/govee_ble_local/icon@2x.png
custom_integrations/govee_ble_local/logo.png
custom_integrations/govee_ble_local/logo@2x.png
```

This folder is a staging area so the assets live with the repo and can be dropped into a
brands PR. The `brands` quality-scale rule stays **todo** until real assets are accepted upstream.
