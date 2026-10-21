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
  warm window after a command whose link the integration kept, when they use
  the link it holds), and a
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
  `ACTIVE_IDLE_DELAY` (30 s) of a command that landed while the integration
  still holds its link (it wasn't released: see the command window below): a
  device doesn't advertise while the library holds a command's link, so its
  poll runs over that link (and if the link has closed meanwhile, its connect
  fails within the 15 s poll budget and counts as a skip). Once released, the
  link no longer counts, and the device is skipped as absent.
- At most `MAX_CONCURRENT_POLLS` polls run at once, integration-wide, and
  each waits at the connect gate (below) behind every waiting command; this
  only stops our own polling from causing a connect storm.
- **Polls give way to commands.** A background poll (scheduled, the first
  after setup, the turn-on poll or the return poll) that comes due while an
  entity command to its device runs, or within
  `POLL_HOLDOFF_AFTER_COMMAND_SECONDS` (25 s) after, waits until then. While
  commands to two or more devices run at once (a light group, an area, "turn
  off all the lights": a burst, `coordinator.CommandBurst`), every device's
  polls wait, until 25 s after the last command to any device ends; a command
  starting meanwhile, to any device, extends it. A turn-off waiting in the
  deferral (below) counts toward it as a command, so one light on and the rest
  already off is a burst too. A poll already running isn't stopped. One poll runs when the wait ends, however many came due (a poll
  that runs meanwhile counts), and a poll waits at most
  `POLL_HOLDOFF_MAX_SECONDS` (2 min). A policy step that stops polls drops a
  held scheduled or turn-on poll, never the first poll or the return poll.
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
- **The link stays up only for a person** (`coordinator._async_end_window`).
  The library keeps a command's link up for its 30 s `ACTIVE_IDLE_DELAY` (and
  its heartbeat keeps the device from ending it sooner), for a follow-up tap
  or slider drag. A device's command window opens as a command to it starts
  and closes `COMMAND_RELEASE_DELAY_SECONDS` (0.5 s) after its last command
  ends; a command meanwhile restarts the wait, so a device's lights in one
  call share one link. The window's close calls `Device.release()` (the link
  and its connection slot go now; the next command reconnects) unless a
  command in it came from a person and no other device's window was open at
  any point during it (`CommandBurst`: a light group, an area, "turn off all
  the lights", a script's steps). A person is the context's user, if that user
  still exists and isn't a system user (`helpers.async_context_is_person`),
  read as the command starts: HA forgets an entity's context on a state write
  5 s after the call. So the dashboard, the companion app, typed Assist, your
  own long-lived tokens (a standalone Node-RED) and Alexa/Google linked
  without the cloud keep the link; voice satellites, automations, HomeKit,
  Cloud Alexa/Google, VoIP and add-ons that reach HA through the Supervisor
  (the Node-RED and AppDaemon add-ons as set up by default: a system user)
  release it. A release waits for the device's operations queued before it,
  and a command started after it queues behind it and reconnects, unless it
  reaches the link first: the library then keeps the link and drops nothing.
  While a poll is reading the device, the release is put off until the poll
  ends (released between the poll's reads, the rest of the poll would connect
  again); a command that starts meanwhile drops the put-off release, and its
  own window decides when it closes. An unload drops a pending close
  (`device.stop()` closes the link anyway).
- A zone light's or zone switch's turn_off sends nothing while the fixture isn't
  known on, no other command to the device is running, and the device confirmed
  that zone off less than `ZONE_OFF_FRESH_SECONDS` (5 s) ago (its ack of the
  fixture's power-off does for every zone; a read-back or the ack of the zone's
  own turn-off does for that zone): turning the fixture and then its zones off
  sends one power-off. That isn't a command (no poll hold-off, no backoff reset).
- **Turn-offs of devices already off go last** (`deferral.py`). While commands
  to other devices run, a whole-fixture light's or a plug's turn-off of a device
  that itself said it is off (`coordinator.power_confirmed_off`: an advertised
  on/off, a read-back or an acked turn-off within `ADVERT_POWER_FRESH_SECONDS`)
  waits until they are done, or `DEFER_OFF_MAX_SECONDS` (10 s), then goes out,
  served after the other commands at the connect gate. So does a zone's
  turn-off while its whole fixture is already off (and it is skipped if the
  fixture's own turn-off confirmed the zone off meanwhile). The wait runs inside
  the service call before the command starts, holding the entity's
  PARALLEL_UPDATES slot, so a later command to the same light can't be undone by
  it; another call queued for that same entity meanwhile sends the turn-off at
  once (`async_request_call`), while the device's other entities queued with it
  (a fixture's zone lights) don't. A waiting zone turn-off is sent at once by a
  call to any of the device's entities (early, it costs one frame to a dark
  fixture). A turn-off of a device whose link is already up (a radio lists it)
  doesn't wait: it would save no slot. A zone's turn-off leaves the device's
  power word as it was (it can't switch the fixture on). Turn-offs of devices already off don't wait for each
  other, and one still waiting when HA stops or the entity is removed is dropped.
- A command that lands drops any poll backoff: the link works again, so the
  next poll comes at the base cadence. After failed polls this happens once
  per failure streak, so polls that keep failing while commands land still
  back off, and never while no polls are scheduled. A command that turns a
  policy-followed light on or off starts or stops its background polls.
- Library and bleak errors become a translated `HomeAssistantError`. A command
  the device refuses (`GoveeBleCommandRejected`) or a rejected secret is also
  logged at WARNING: the library logs it only at DEBUG.

### Connect gate (`connect_gate.py`)
A mass command ("turn off all the lights") starts every device's connect
within milliseconds, and habluetooth trusts the free-slot count a proxy last
reported: a 3-slot ESPHome proxy was handed five connects, refused two, and
bleak-retry-connector sent those down whatever path was left (a local adapter
at -96 dBm that waited out a 20 s timeout). So each command's library call,
and each background poll, first takes a ticket from one integration-wide gate:
- A device whose link is up (it is in a radio's allocated list), or whose other
  call is connecting, goes at once. Otherwise it goes when the radio of the path
  habluetooth would pick now (its own scoring) has room: a free slot once our
  connects it hasn't listed yet are counted, and for a proxy fewer than
  `CONNECT_GATE_IN_FLIGHT_PER_PROXY` (1) connects in progress through it, anyone's
  (an ESPHome proxy jams on concurrent connects while a slot is connecting). A
  local adapter takes as many as it has free slots: BlueZ runs pending connects
  side by side.
- That path must be usable for the device: RSSI at or above
  `CONNECT_GATE_RSSI_FLOOR` (-85 dBm), fewer than `CONNECT_GATE_PATH_FAILURES`
  failed connects there since one worked, and its radio not avoided. If it isn't,
  the device waits only while a busy usable path is heard better than the
  predicted one scores (once free, habluetooth may pick it); otherwise waiting
  can't change the pick and it goes.
- A device whose busy best path has a usable alternative doesn't wait for it
  (habluetooth's score moves to the next path once it sees the busy radio's
  connect); one with no usable alternative waits for its radio, first in line.
  Commands go before polls, and turn-offs of devices already off after the
  other commands; a poll needing a connect waits while any command does, a
  turn-off waiting in the deferral included.
- A radio that has heard no advertisement for `CONNECT_GATE_DEAF_SECONDS` (10 s)
  while not connecting normally (nothing in progress, or a connect with no slot
  listed) is avoided for `CONNECT_GATE_AVOID_SECONDS` (5 min), or until one of
  our connects comes up through it: a proxy whose connects hang goes deaf while
  they do, though it may report every slot free, and must not draw the devices
  whose better path is only busy for a second. For
  `CONNECT_GATE_RELEASE_GRACE_SECONDS` (3 s) after one of our calls through a
  radio ends, no pass judges it stuck (a proxy pauses scanning while it
  connects); a call that failed has its radio checked once that grace is over,
  at the next call's pass, and one that worked queues no such check.
- Radios are re-read every `CONNECT_GATE_TICK_SECONDS` (0.25 s) while anything
  waits (HA has no slot-freed callback); a call waits at most
  `CONNECT_GATE_MAX_WAIT_SECONDS` (12 s), then goes as it would without the gate,
  one per radio per tick and not to a radio another call was just admitted to.
  If reading the scanners fails, the waiting calls go ahead unjudged and the
  error is logged once. When an entry unloads or HA stops, its device's calls
  still waiting go ahead at once and get a turn of the event loop, so they reach
  the device before its link closes (as one already using the link does).
- It can't force habluetooth's path choice (made again inside the connect), and
  can't see bleak-retry-connector's retries within one connect. A poll waits at
  the gate holding one of the `MAX_CONCURRENT_POLLS` places. The self-test isn't
  gated.

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
  A segment shows off while its fixture is known off, and turning it on then
  switches the fixture on first. On an H60A6 or H6047 that isn't known on, a
  segment's turn_off sends nothing, because a colour frame there switches the
  fixture on: the segment comes back in its old colour with the fixture. On
  other models it is blacked out; whether that colour frame wakes a fixture that
  is off (an H61A8 or H6641, say) hasn't been checked on the devices.
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
- Enable `custom_components.govee_ble_local.connect_gate: debug` for one line
  per command and poll: how it got through the connect gate (`admitted`,
  `linked`, `joined`, `unseen`, `capped`, `unjudged` or `released`, the last when
  its entry unloads or Home Assistant stops), how long it waited, the
  radio its connect was expected on (and where it turned up, if elsewhere), and
  how long the call took after that. Alongside, `habluetooth.wrappers: info`
  logs each connect's ranked paths (`debug` also the path it connected through),
  and `bleak_retry_connector` / `bleak_esphome.backend.device` at debug each
  attempt and each proxy's slot reports.
- The **Connection source** sensor shows which connectable adapter/proxy Home
  Assistant currently prefers for reaching the device (unknown while no
  connectable scanner hears it); a proxy that drops offline shifts all traffic
  to the remaining scanners.
