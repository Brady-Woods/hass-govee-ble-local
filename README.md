# hass-govee-ble-local

**Local Bluetooth LE control of Govee lights for Home Assistant — no cloud, no LAN API.**

This is a native Home Assistant integration that controls Govee lights
**directly over Bluetooth Low Energy**. No Govee account, no cloud polling, no
MQTT bridge, and no dependence on the (limited) LAN API — just Home Assistant
talking to the device over BLE, using a reverse-engineered implementation of
Govee's encrypted BLE control protocol.

> ⚠️ **Unofficial.** Not affiliated with or endorsed by Govee. The protocol was
> reverse-engineered from packet captures; behavior can change with device
> firmware. Use at your own risk.

## Why

Govee's official Home Assistant story is cloud- or Matter-based, both of which
are limited: the cloud path depends on Govee's servers and rate limits, and
Matter exposes only basic on/off/brightness/color — no zones, no segments, no
scene library (see the protocol notes for details). This project goes direct to
the device over BLE to unlock the full feature set locally.

## Supported devices

| Model | Status |
| --- | --- |
| **H60A6** (Ceiling Light Pro) | ✅ power, brightness, RGB, color temp, scenes; two **independently controllable zones** (main panel / background ring) with per-zone RGB **and** kelvin; 13 addressable segments; full status read-back |
| **H6047** (light bar) | ✅ power, brightness, RGB, color temp, scenes; left/right bar zones; 10 segments |
| **H6641** (RGBIC strip) | ✅ power, brightness, RGB, color temp, scenes; addressable segments |
| **H61A8** (LED rope) | ✅ power, brightness, RGB, scenes; 15 addressable segments (RGB only — no color temp) |
| **H6006**, **H6008**, **H6052** (RGBWW bulbs / lamp) | ✅ power, brightness, RGB, color temp, scenes (state tracked optimistically where the device has no read-back) |
| **H5083** & plug family (H5080/82/85/89, H5160/61) | ✅ on/off, exposed as a switch (with read-back where available) |
| **H5122** (button sensor) | ⏳ Deferred — no verified local BLE format (events are cloud-push); see `custom_components/govee_ble_local/GAPS.md` |

The goal is to grow this list. The BLE protocol logic lives in a standalone,
device-agnostic library (see **Architecture**), so adding a model is mostly a
matter of describing its capabilities in a profile, not re-implementing the
transport. The curated devices above double as the **real-hardware test set**.

## Features

- Fully local control over BLE — works with airplane mode / no internet
- On/off, brightness, RGB color, color temperature
- Built-in scene/effect library (with an "off" entry to clear an effect)
- Independent per-**zone** control (RGB **and** kelvin) where the fixture has physical zones
- Per-**segment** lights (RGB + kelvin), registered disabled-by-default (opt in per segment)
- On/off tracked **passively from advertisements** between polls (no connection needed)
- Diagnostics: signal strength, connectivity (advertisement presence), last-seen / last-connected
  timestamps, poll interval, connection-failure count — plus a **self-test** button and a
  `capture_session` service that exercises the device and returns a captured BLE session
- Bluetooth auto-discovery, config flow, device diagnostics, full translations

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add this repo's URL, category **Integration**.
2. Install **Govee BLE Local**, then restart Home Assistant.
3. The light is auto-discovered over Bluetooth; confirm it under
   **Settings → Devices & Services**.

### Manual
Copy `custom_components/govee_ble_local/` into your Home Assistant `config/custom_components/`
directory and restart Home Assistant.

Requires a working Bluetooth adapter (or an ESPHome Bluetooth proxy) in range
of the light.

### The `govee-ble-local` library dependency
The BLE protocol library is **not published to PyPI yet**. Home Assistant installs it
automatically at startup from the Git URL in `manifest.json` (`requirements`), so no manual
`pip install` is needed — HA re-installs it on each start, which keeps it present even after a
container image update. It stays within Home Assistant's pinned dependency versions (e.g.
`cryptography`) so it won't disturb your install.

### Upgrading
- **HACS:** open the integration in HACS → **Update** → restart Home Assistant.
- **Manual:** delete the old `custom_components/govee_ble_local/` folder, drop in the new release,
  and restart (deleting first avoids leaving removed files behind).
- After upgrading, confirm the integration still loads (Settings → Devices & Services) and your
  entities are present.

See **[INSTALL.md](INSTALL.md)** for containerized Home Assistant (Docker/Podman) specifics,
including the SELinux/ownership steps needed when copying files into a bind-mounted `config`
directory.

Requires a working Bluetooth adapter (or an ESPHome Bluetooth proxy) in range of the light.

## Entities & services

Per device (depending on its capabilities):

| Platform | Entity | Notes |
| --- | --- | --- |
| `light` | Main light | Whole-fixture power/brightness/RGB/color-temp/effects |
| `light` | Zone light(s) | One per colour-controllable zone (RGB + kelvin); on/off-only zones become switches |
| `light` | Segment light(s) | One per addressable segment (RGB + kelvin); **disabled by default** |
| `switch` | Power / zone switch | Smart plugs, and on/off-only zones |
| `sensor` | Signal strength, poll interval, connection failures, **last seen**, **last connected**, **connection source** | Diagnostic |
| `binary_sensor` | Connectivity | Advertisement presence (reachable), not connect-poll success |
| `button` | Run self-test | Runs the full-capability self-test and stores the capture |

**Service — `govee_ble_local.capture_session`** (target one or more devices): runs the device
self-test (power/brightness/RGB/kelvin/scenes/segments/zones with per-step ACK + read-back
checks) and returns the captured BLE frame session as response data, one report per targeted
device (each identifying its own address, so results from multiple devices are never ambiguous)
— useful for diagnosing protocol issues. Before making any changes it snapshots the device's full
state (power, brightness, colour, active scene, every zone's power/colour, segment colour, the
gradual flag) and restores all of it when the test finishes. The same capture is stored for the
config-entry diagnostics download.

## Architecture

The BLE control protocol (encryption, handshake, command framing, status
parsing) and per-model device profiles live in a standalone Python package,
[`govee-ble-local`](https://github.com/Brady-Woods/govee-ble-local), which this
integration depends on. That keeps the reusable device logic independent of
Home Assistant and testable on its own, with this repo providing the thin HA
adapter layer (entities, config flow, coordinator). Discovery matches any Govee
device by Bluetooth manufacturer ID; the library's profile system then decides
which models are supported.

## Documentation

- [Integration guide](custom_components/govee_ble_local/README.md) — architecture, entities, troubleshooting, known issues
- [Protocol reference](https://github.com/Brady-Woods/govee-ble-local/blob/master/PROTOCOL.md) — the full reverse-engineered BLE protocol (in the `govee-ble-local` library)

## License

[MIT](LICENSE) © Brady Woods
