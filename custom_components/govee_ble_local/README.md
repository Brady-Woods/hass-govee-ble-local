# Govee BLE Local — Home Assistant integration

A Home Assistant custom component that controls Govee lights and plugs directly
over Bluetooth LE — no cloud, no MQTT bridge, no hub. It is a **thin Home
Assistant adapter** over the standalone
[`govee-ble-local`](https://github.com/Brady-Woods/govee-ble-local) library,
which implements the (reverse-engineered) BLE protocol, the encrypted
transport and the per-model device profiles.

This document covers the **Home Assistant layer** — how it wires the library
into HA. The protocol reference lives in the library (`spec/docs/`).

## Supported devices & entities

Discovery matches Govee manufacturer IDs and local-name prefixes (see
`manifest.json`); the library's profile table decides which models are
supported (`govee_ble_local.supported_skus()`). Entities are driven by each
profile's capabilities:

| Entity | Type | Notes |
| --- | --- | --- |
| Main light | `light` | power, brightness, RGB, colour temperature, scenes (effects) |
| Zone lights | `light` | one per colour-controllable zone (H60A6 main/background, H6047 left/right bar) |
| Zone switches | `switch` | one per on/off-only zone |
| Segment lights | `light` | one per addressable segment, **disabled by default** |
| Power switch | `switch` | only for a power-only device (smart plug) |
| Diagnostics | `sensor`/`binary_sensor`/`button` | RSSI, connectivity, failures, poll interval, last seen/connected, connection source, self-test; RSSI, poll interval and last seen/connected are **disabled by default** |

## Architecture

### Setup (`__init__.py`)
1. Resolve the `BLEDevice` from HA's Bluetooth manager (`ConfigEntryNotReady`
   if no connectable scanner sees the device) and the SKU (stored on the entry,
   else identified from the advertisement).
2. `create_device()` from the library (with the secret key for secret-gated
   plugs) and a `GoveeBleLocalCoordinator`.
3. Mark the entry ready from passive state and run the **first poll in the
   background**, staggered by `crc32(address) % 30` s — a full connect +
   handshake for every device on every restart is what exhausts connection
   slots.
4. Register a passive advertisement callback for any scanner (connectable or
   not): updates on/off from the advertisement and, from a connectable scanner,
   keeps the library's `BLEDevice` fresh (best proxy path for the next
   connection). Device state goes to every entity only when it changed. HA
   calls it only when an advertisement's content changes, so last-seen/RSSI
   are read from HA's advertisement history once a minute instead (and at
   once when the device is heard again after no scanner did). Passive updates
   never reschedule the poll. Loss of the connectable and of the any-scanner
   history are tracked separately.

### Polling (`coordinator.py`)
- `Device.update()` every `POLL_INTERVAL_SECONDS` (120 s) + jitter; capped
  exponential backoff on failure (`MAX_POLL_INTERVAL_SECONDS`). The poll
  interval sensor shows the interval without the jitter, and is unknown while
  no polls are scheduled (polling turned off, or halted by a rejected key).
- A device no connectable scanner can see is **not connected to** (the poll
  fails fast instead of spending ~1 min on hopeless retries).
- At most `MAX_CONCURRENT_POLLS` polls run at once, integration-wide. HA's
  Bluetooth manager already routes each connection to a scanner with a free
  slot; this only stops our own polling from causing a connect storm.
- **Availability = reachability**: an entity stays available while the device
  is advertising, regardless of poll failures (commands can still reach it).
  A device that vanished from every connectable scanner goes unavailable
  after `UNAVAILABLE_AFTER_FAILURES` failed polls, or as soon as it vanishes
  when no polls are scheduled (polling turned off in the entry's system
  options, or halted by a rejected key). When it starts advertising again it
  is polled at once and the backoff is reset.
- A rejected secret key (`GoveeBleAuthError`) starts the re-auth flow, and
  polling stops until the entry is reloaded with a new key.

### Commands (`light.py`, `switch.py`, `entity.py`)
- The main light sends power + brightness + colour as **one batched library
  operation** (`Device.turn_on`) — one lock, one connection, each frame
  ack-verified, power first. Whole-fixture power is the library's concern
  (global switch, per-zone frames on H60A6, one combined frame on H6047).
- After every command all of the device's entities are refreshed together
  (whole-fixture off also updates the zone lights/switches).
- A command that lands drops any poll backoff: the link works again, so the
  next poll comes at the base cadence. This happens once per failure streak,
  so polls that keep failing while commands land still back off, and never
  while no polls are scheduled.
- Library and bleak errors become a translated `HomeAssistantError`.

## Data updates

Polled every 120 s (plus passive on/off from advertisements in between). What
is read back depends on the model's profile: power, brightness, zone power,
active scene, colour temperature, per-segment colour (where supported) and
device info (firmware/hardware version, serial, Wi-Fi MAC).

## Known limitations

See [`GAPS.md`](GAPS.md) for features that are implemented but not yet
verified on hardware.

- The friendly name you set in the Govee app is cloud-only; rename the device
  in HA after adoption.
- Segment lights model "off" as black (the protocol has no per-segment power).
- A device can wedge its BLE stack so only a mains power-cycle recovers it;
  that looks like an integration fault but isn't.

## Configuration

No YAML. Add via Bluetooth discovery or **Add Integration → Govee BLE Local**.
Secret-gated plugs need their 8-byte key: read off an unbound device
automatically, fetched from your Govee account (credentials used once, not
stored), or entered manually. **Reconfigure** replaces the key (a blank field
keeps the stored one; a checkbox removes it) and reloads; **diagnostics** are
downloadable per device (MACs, serial and secret redacted by key and scrubbed
from all free text in either byte order; the entry title is redacted too, and
so is every hex run of 8+ characters in the stored self-test report and the
recent warnings, so an identifier split across frame chunks can't leak).

## Troubleshooting

- Enable `govee_ble_local.frames: debug` to log every TX/RX frame; the
  **Run self-test** button / `capture_session` service exercises every
  capability and returns the captured session.
- The **Connection source** sensor shows which connectable adapter/proxy Home
  Assistant currently prefers for reaching the device (unknown while no
  connectable scanner hears it); a proxy that drops offline shifts all traffic
  to the remaining scanners.
