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
| **H60A6** (Govee Ceiling Light Pro) | ✅ Supported — power, brightness, RGB, color temperature, scenes, and independent upper-ring / lower-panel zones |

The goal is to grow this list. The BLE protocol logic lives in a standalone,
device-agnostic library (see **Architecture**), so adding a model is mostly a
matter of describing its capabilities in a profile, not re-implementing the
transport.

## Features

- Fully local control over BLE — works with airplane mode / no internet
- On/off, brightness, RGB color, color temperature (2700–6500 K)
- Built-in scene/effect library
- Independent control of the fixture's two physical zones (upper ring, lower panel)
- Bluetooth auto-discovery, config flow, diagnostics, full translations

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add this repo's URL, category **Integration**.
2. Install **Govee BLE Local**, then restart Home Assistant.
3. The light is auto-discovered over Bluetooth; confirm it under
   **Settings → Devices & Services**.

### Manual
Copy `custom_components/govee_h60a6/` into your Home Assistant `config/custom_components/`
directory and restart.

Requires a working Bluetooth adapter (or an ESPHome Bluetooth proxy) in range
of the light.

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

- [Integration guide](custom_components/govee_h60a6/README.md) — architecture, entities, troubleshooting, known issues
- [Protocol reference](https://github.com/Brady-Woods/govee-ble-local/blob/master/PROTOCOL.md) — the full reverse-engineered BLE protocol (in the `govee-ble-local` library)

## License

[MIT](LICENSE) © Brady Woods
