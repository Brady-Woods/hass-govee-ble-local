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
   never postpone the poll; they start or stop background polls only when they
   move the device to another step of the poll policy (below). Loss of the
   connectable and of the any-scanner history are tracked separately.

### Polling (`coordinator.py`)
- Background polls (`Device.update()`) follow one per-device policy
  (`_async_policy_step`). A device's advertisement carries its on/off state
  and nothing else, so while Home Assistant has heard that byte in the last
  `ADVERT_POWER_FRESH_SECONDS` (180 s):
  - a light that is **off** gets no background polls;
  - a light turned **on outside Home Assistant** (the advertisement flips to
    on, outside the library's 15 s suppression after HA's own commands and
    reads) is polled once `ADVERT_FLIP_POLL_DELAY_SECONDS` (5 s) later; a
    newer flip replaces a pending poll. Not while Home Assistant is stopping,
    after a rejected key, or while no entity listens; a device back in reach
    gets its return poll instead;
  - a light that is **on** is polled from `POLL_INTERVAL_SECONDS` (120 s),
    doubling after each poll that read nothing new up to
    `ON_POLL_MAX_INTERVAL_SECONDS` (15 min), and back to 120 s on any change:
    a poll that reads something new, an HA command, or an advertised flip
    (the turn-on poll after it always counts as new);
  - a **relay plug** (H5083 family) gets no background polls: the relay is
    its only state.
- Everything else keeps the **fixed cadence** of 120 s + jitter: a device whose
  byte isn't heard (an H6006 heard only by a passive scanner, which never gets
  its scan response) or has gone stale, one no connectable scanner sees (its
  polls are skipped and counted as before, except within the library's 30 s
  warm window after a command landed, when they use the link it holds), and a
  model in `EXCLUDED_FROM_ADVERT_POLICY` (H60A6 and H60A1: a disconnect may
  end a music or group mode started over Wi-Fi; static finding, bench check
  pending). The policy is re-checked on every advertisement callback and
  presence tick (every 60 s).
  Within a minute of its byte going stale, a device is back on the fixed
  cadence; its first poll follows one base interval (120 s plus up to 30 s of
  jitter) after that.
- HA calls the advertisement callback only when the content changes, so an
  on/off change the library ignored (inside its 15 s window after HA's own
  command or read), or one whose advertisement matches what HA heard before a
  command, would never reach it again. The presence tick therefore offers HA's
  newest advertisement to the library again (`_async_reoffer_advertisement`),
  but only one received more than that window after HA last talked to the
  device (a poll, a command or a self-test): one from before the command,
  still in the history while the link is up, is never offered again. Each
  advertised content can change on/off this way once (until it changes or HA
  commands the device), so a poll that reads the opposite isn't overridden
  every tick.
- Failed or skipped polls back off as before: capped exponential backoff up to
  `MAX_POLL_INTERVAL_SECONDS` (30 min). Polling disabled in system options or
  halted by a rejected key stays off whatever the advertisements say.
- The poll interval sensor shows the interval without the jitter, and is
  unknown while no background polls are scheduled (polling turned off, halted
  by a rejected key, or off under the policy).
- A device no connectable scanner can see is **not connected to** (the poll
  fails fast instead of spending ~1 min on hopeless retries), except within
  `ACTIVE_IDLE_DELAY` (30 s) of a command that landed: a device doesn't
  advertise while the library holds a command's link, so its poll runs over
  that link (and if the link has closed meanwhile, its connect fails within
  the 15 s poll budget and counts as a skip).
- At most `MAX_CONCURRENT_POLLS` polls run at once, integration-wide. HA's
  Bluetooth manager already routes each connection to a scanner with a free
  slot; this only stops our own polling from causing a connect storm.
- **Availability = reachability**: an entity stays available while the device
  is advertising, regardless of poll failures (commands can still reach it).
  A device that vanished from every connectable scanner goes unavailable
  after `UNAVAILABLE_AFTER_FAILURES` failed polls or once it hasn't been heard
  or reached (a command that landed, or a poll that didn't fail once the device
  has been read back at least once) for
  `UNAVAILABLE_AFTER_UNHEARD_SECONDS`, whichever comes first, or as soon as it
  vanishes when no background polls are scheduled (polling turned off in the entry's
  system options, halted by a rejected key, or off under the policy); polls
  resuming before it is heard again don't make it available. When it starts
  advertising again after skipped polls it is polled at once and the backoff
  is reset.
- A rejected secret key (`GoveeBleAuthError`) starts the re-auth flow, and
  polling stops until the entry is reloaded with a new key. It is logged at
  WARNING, so it shows in the diagnostics' captured log.

### Commands (`light.py`, `switch.py`, `entity.py`)
- The main light sends power + brightness + colour as **one batched library
  operation** (`Device.turn_on`) — one lock, one connection, each frame
  ack-verified, power first. Whole-fixture power is the library's concern
  (global switch, per-zone frames on H60A6, one combined frame on H6047).
- After every command all of the device's entities are refreshed together
  (whole-fixture off also updates the zone lights/switches).
- A command that lands drops any poll backoff: the link works again, so the
  next poll comes at the base cadence. After failed polls this happens once
  per failure streak, so polls that keep failing while commands land still
  back off, and never while no polls are scheduled. A command that turns a
  policy-followed light on or off starts or stops its background polls.
- Library and bleak errors become a translated `HomeAssistantError`. A command
  the device refuses (`GoveeBleCommandRejected`) or a rejected secret is also
  logged at WARNING: the library logs it only at DEBUG.

## Data updates

On/off comes passively from advertisements. Background polls read the rest:
none while a light is off or for a relay plug, once 5 s after a light is turned
on outside Home Assistant, then every 2 min backing off to 15 min while it
stays on and nothing changes (see Polling above). Devices the policy doesn't
cover (H60A6, H60A1, a device whose on/off byte isn't heard, or one no
connectable scanner sees) are polled every 120 s. What is read back depends on
the model's profile: power, brightness, zone power, active scene, colour
temperature, per-segment colour (where supported) and device info
(firmware/hardware version, serial, Wi-Fi MAC).

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

- The diagnostics download includes the device's recent WARNING+ log lines:
  unrecognised frames and empty status reads from the library, rejected
  commands and secrets and failed polls from this integration.
- Enable `govee_ble_local.frames: debug` to log every TX/RX frame; the
  **Run self-test** button / `capture_session` service exercises every
  capability and returns the captured session.
- The **Connection source** sensor shows which connectable adapter/proxy Home
  Assistant currently prefers for reaching the device (unknown while no
  connectable scanner hears it); a proxy that drops offline shifts all traffic
  to the remaining scanners.
