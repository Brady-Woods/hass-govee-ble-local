# Govee BLE Local — Home Assistant integration

A native Home Assistant custom component that controls Govee lights and
plugs directly over Bluetooth LE — no cloud, no MQTT bridge, no hub. It is a
**thin Home Assistant adapter** over the standalone
[`govee-ble-local`](https://github.com/Brady-Woods/govee-ble-local) library,
which implements the (reverse-engineered) BLE protocol and holds the per-model
device profiles.

Exposes each device as either a `light` entity (power, brightness, RGB, color
temperature, scenes/effects) plus one `switch` per physical zone (H60A6: an
upper ring and a lower panel) - or, for a profile with no light-relevant
capability at all (H5083, a smart plug), a single whole-device `switch`
instead.

> The full BLE protocol reference now lives in the library:
> [`govee-ble-local/PROTOCOL.md`](https://github.com/Brady-Woods/govee-ble-local/blob/master/PROTOCOL.md).
> This document covers the **Home Assistant integration layer** — how it wires
> the library into HA.

## Supported devices & entities

Discovery matches **any Govee device by Bluetooth manufacturer ID (`0x8843`)**;
the library's **device-profile system** then decides which models are actually
supported. Unsupported models abort the config flow with "not supported yet".
Six models currently ship a profile: **H60A6** (ceiling light, zones),
**H61A8** (LED rope/strip, 20 segments with real read-back), **H6006**/
**H6052**/**H6008** (RGBWW bulbs), and **H5083** (smart plug, on/off only).
Not every device has a working live status read-back - see each SKU's
`NOTES.md` in the library for what's confirmed; where it's missing, RGB/
color-temp/scene state is tracked optimistically from the last command sent
rather than polled.

Entities created per device are driven by the profile's capabilities:

| Entity | Type | Notes |
| --- | --- | --- |
| Main light | `light` | on/off, brightness, RGB, color temp, scenes/effects - skipped entirely for a profile with none of these |
| Zone switches | `switch` × *n* | one per `capabilities.zones` (H60A6: "Upper ring", "Lower panel") |
| Whole-device power switch | `switch` | only for a profile with no light capability *and* no zones (H5083) - on/off is otherwise handled by the light entity or zone switches above |

Per-segment control exists in the library but is **not** exposed as its own
set of HA entities (individual per-segment set commands aren't wired to
entities on any device) - though H61A8's confirmed, real per-segment status
read-back (`get_segment_status()`) does feed the main light entity's on/off
and brightness state, since H61A8 has no other working status mechanism. On
H60A6, the fuller status query needed for the same per-segment data is too
drop-prone under real adapter contention to build on for the whole light
entity's core on/off state - see the library's PROTOCOL.md §5.3.2.

## Architecture / execution flow

The heavy lifting is in the library; this integration is the glue.

### Setup (`__init__.py`)
1. Resolve the `BLEDevice` for the entry's address from HA's Bluetooth manager
   (`ConfigEntryNotReady` if not currently visible).
2. **Resolve the device profile** from `govee_ble_local.profile`: prefer the SKU
   stored on the config entry, else match the advertised local name, else fall
   back to the default SKU. YAML loading runs in the executor.
3. Create a `GoveeBleClient` (from the library) and a `GoveeBleLocalCoordinator`.
4. Stagger the first poll by `crc32(address) % 8` seconds so multiple lights
   don't poll in lockstep and fight over the adapter's connection slots.
5. `async_config_entry_first_refresh()`, then fetch the serial number once
   (best-effort; a failure is logged, not fatal).
6. Store `client`, `coordinator`, `profile`, `serial_number` in
   `entry.runtime_data` and forward the `light` + `switch` platforms.

### Coordinator polling (`coordinator.py`)
A `DataUpdateCoordinator` polls `client.get_status()` every
`POLL_INTERVAL_SECONDS` (60s). `BleakError` (drops, no-response, out-of-slots)
is converted to `UpdateFailed` so it's treated as an expected transient, not an
"unexpected error" traceback.

### Device registry self-heal (`__init__.py`)
`device_info` on entities is only applied at first registration, so a bad value
from one flaky early poll would stick forever. The setup registers a coordinator
listener that re-syncs the device's `connections` on every successful poll using
`async_update_device(new_connections=...)` — a **full replace**, not the merge
semantics of `async_get_or_create` (which only ever *adds* connections and once
left a stale/garbled MAC alongside the correct one).

### BLE device refresh
A passive `bluetooth.async_register_callback` keeps the client's `BLEDevice`
current as new advertisements arrive.

## Entities

### Main light (`light.py` — `GoveeBleLocalLight`)
- **Color modes / temp range** come from the profile capabilities (RGB and/or
  color temp; H60A6 = both, 2700–6500 K).
- **`is_on`** = any of the fixture's zones on (from polled `zone_*_on`).
- **`brightness`** from the polled `brightness_pct`.
- **`effect`** maps the polled `scene_id` to a scene name via the profile.
- **RGB and color temp are tracked optimistically** from the last command sent
  — the short status query has no color read-back. (The library *can* read RGB
  back via the fuller segment query, but the integration uses the short, more
  reliable query.)
- **Effect list** = the profile's *selectable* scenes (broken ones excluded).

### Zone switches (`switch.py` — `GoveeBleLocalZoneSwitch`)
One per `profile.capabilities.zones`, mapped to a BLE zone index and translation
key via `const.ZONE_META`. `is_on` reads the corresponding polled zone flag.

## Scenes

Scene data (name → code + base64 `scenceParam`) comes from the **library device
profile** (`devices/h60a6/scenes.yaml`), not a runtime cloud fetch. Activation:
- if the scene has upload data (`param`), do a full `set_scene_full()` upload
  (reliable regardless of the device's cache), else
- fall back to bare `set_scene()` activation (works only if the device already
  cached that scene).

Scenes that don't render correctly over BLE are flagged `working: false` in the
profile; they're hidden from the effect picker, and directly requesting one
raises a clear error. See the library's PROTOCOL.md for the two root causes
(`0xFF`-placeholder headers and oversized payloads).

## Device identity: name & MACs

- The HA **device name** defaults to the BLE **local name** (e.g.
  `GVH60A67457`), surfaced during discovery via `flow_title`. The friendly
  nickname you set in the Govee app ("Hall Ceiling 1") is **cloud-only** and not
  available over BLE — rename the device in HA after adoption.
- The device registry records both the **BLE MAC** and the **Wi-Fi MAC** (they
  differ by one in the last octet), which lets HA correlate this device with
  other integrations (e.g. your router).
- The **serial number** is read once over BLE and shown in device info.

## Data updates

Local polling every **60 seconds** over BLE (plus an on-demand refresh after
each command). Read back from the device: zone on/off, brightness, current
scene, Wi-Fi MAC, and hardware version. RGB and color temperature are **not**
read back (tracked optimistically).

## Known issues

- **Some scenes are hidden** (flagged `working: false` in the profile) because
  they don't render over BLE — see the library PROTOCOL.md.
- **Zone on/off read-back** decodes bytes 14 (lower) / 15 (upper) of the status
  terminator chunk; the encoding was historically tricky (see PROTOCOL.md
  §5.2) — the current byte mapping is confirmed by a live 4-state truth table.
- **"Light won't turn on/off" isn't always this integration.** A device can get
  into a wedged BLE state that only a mains power-cycle clears; it looks similar
  but is a device fault, not a decode bug.

## Configuration

No YAML/options configuration. The device is added via Bluetooth discovery or
**Add Integration → Govee BLE Local**. A **reconfigure** action re-verifies the
device is reachable over BLE and reloads it (a quick recovery without deleting
and re-adding). **Diagnostics** are downloadable per device (MAC/serial
redacted; includes the resolved profile capabilities).

## File structure

```
__init__.py       Setup: resolve profile, build runtime_data, register platforms.
config_flow.py    Bluetooth (manufacturer-id) + manual + reconfigure flow;
                  profile-gated (aborts unsupported models).
const.py          HA-only constants: DOMAIN, POLL_INTERVAL_SECONDS, ZONE_META.
coordinator.py    DataUpdateCoordinator over the library client.
diagnostics.py    Config-entry diagnostics (MAC/serial redacted).
entity.py         Shared base entity: error-wrapping helper, device_info.
light.py          Main light entity (GoveeBleLocalLight).
switch.py         Per-zone switch entities (GoveeBleLocalZoneSwitch).
manifest.json     Manifest: manufacturer-id bluetooth matcher, govee-ble-local
                  requirement, quality_scale.
strings.json      Config-flow + entity + exception strings (translations/en.json).
icons.json        Zone-switch icon translations.
quality_scale.yaml  Per-rule Integration Quality Scale tracking.
PROTOCOL.md       Pointer to the protocol reference (now in the library).
test_config_flow.py  Standalone config-flow tests (stdlib-only).
```

The BLE protocol implementation and its tests live in the
[`govee-ble-local`](https://github.com/Brady-Woods/govee-ble-local) library.
