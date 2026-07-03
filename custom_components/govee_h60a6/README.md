# Govee H60A6 Ceiling Light — Home Assistant Integration

A native Home Assistant custom component for the Govee H60A6 ceiling light,
controlling it directly over Bluetooth LE (no cloud, no MQTT bridge, no
hub). Exposes the light as a `light` entity (power, brightness, RGB, color
temperature, scenes/effects) plus two `switch` entities for independent
control of the fixture's two physical zones (the upper ring and lower
panel).

This document covers **how the integration is put together and how it
runs**. For the BLE protocol itself — every opcode, the encryption scheme,
and the full history of what's been reverse-engineered, tested, and is
still unresolved — see [`PROTOCOL.md`](./PROTOCOL.md). That document is
the primary technical record of this project; this README is oriented
around the *code*, not the *wire format*.

## Architecture / execution flow

### Startup (`__init__.py`)

1. `async_setup_entry` resolves the config entry's stored BLE address to a
   live `BLEDevice` via Home Assistant's Bluetooth integration
   (`bluetooth.async_ble_device_from_address`). If the device isn't
   currently visible to any Bluetooth adapter HA knows about, setup fails
   with `ConfigEntryNotReady` (HA will retry automatically).
2. One `GoveeH60A6Client` and one `GoveeH60A6Coordinator` are created per
   config entry (per physical light) and stored in
   `hass.data[DOMAIN][entry.entry_id]`. **Both the `light` and `switch`
   platforms share the same client instance** for a given device — this
   matters because the client serializes all BLE operations for that
   device behind a single `asyncio.Lock`, so a scene upload from the light
   entity and a zone toggle from a switch entity can never race each other
   on the wire.
3. The scene library is fetched once at startup from Govee's public,
   unauthenticated API (`scene_library.async_fetch_scene_library`) and
   passed to the light entity. If that fetch fails (no internet, API
   shape change), the integration falls back to a static, hand-captured
   scene table (`const.SCENES`) that only supports **bare** activation
   (see below) rather than full data upload.
4. Multiple lights' first polls are staggered (`stagger = crc32(address) %
   8` seconds) so they don't all hit the Bluetooth adapter at the exact
   same moment on startup.
5. A coordinator listener re-syncs the HA device registry
   (`_sync_device_registry`) on every successful poll, not just once at
   startup — this makes a bad read (e.g. two lights' BLE traffic briefly
   cross-contaminating right after boot) self-correct on the next good
   poll instead of leaving stale MAC/hardware-version data stuck in the
   registry forever.

### Per-device connection lifecycle (`client.py`)

`GoveeH60A6Client` maintains an **on-demand** encrypted BLE session per
device, not a permanently-open connection:

- Every public method (`get_status`, `send_command`, `set_scene_full`,
  ...) acquires `self._lock`, calls `_connect()` (a no-op if already
  connected), does its work, then calls `_schedule_disconnect()` — a
  2-second idle timer that tears the connection down if nothing else uses
  it in that window. This keeps the adapter's limited connection slots
  free for other devices (including HA's own general-purpose BLE scanning)
  most of the time, at the cost of a ~300-500ms reconnect handshake
  whenever a new operation starts cold.
- `_connect()` establishes the GATT connection (via
  `bleak_retry_connector.establish_connection`, which retries transient
  failures automatically), subscribes to the notify characteristic, and
  runs the encryption handshake (`_handshake`) to derive a per-session AES
  key from the device's own challenge response.
- Every write path drains any stale queued notification
  (`_drain_notify_queue`) immediately before writing. This matters: a
  late-arriving notification from a *previous* operation sitting in the
  queue would otherwise get misattributed as the ack for an unrelated
  *next* operation, and vice versa — a real bug found and fixed via live
  testing (see `PROTOCOL.md` §6.2.1).
- A missing/late command ack is **not** treated as a hard failure for
  scene uploads (it used to be, and that was itself a bug — see the same
  section). The write already went out; an absent ack doesn't prove it
  failed, and treating it as fatal caused real, measured reliability
  regressions under repeated use.

### Coordinator polling (`coordinator.py`)

A `DataUpdateCoordinator` subclass polls `client.get_status()` on a fixed
interval (`const.POLL_INTERVAL_SECONDS`, currently 30s) so Home Assistant
stays roughly in sync with changes made from the Govee app or a physical
switch, not just changes made through HA itself. `BleakError` during a
poll is converted to `UpdateFailed`, which HA treats as an expected,
recoverable failure mode (brief unavailability) rather than logging a full
traceback as an "unexpected error."

### Entities

- **`light.py`** (`GoveeH60A6Light`) — the primary entity. Power is
  derived from `zone_upper_on OR zone_lower_on` (either zone being on
  counts as "the light is on"). Brightness and scene/effect are read from
  the polled status. RGB color and color temperature have **no BLE
  readback path at all** — the device doesn't expose them via status
  query — so they're tracked optimistically from the last command sent,
  the same pattern used by most write-only BLE light integrations.
- **`switch.py`** (`GoveeH60A6ZoneSwitch`, one per zone) — independent
  on/off control for the upper ring and lower panel, for cases where the
  main light entity's combined on/off isn't granular enough (e.g. an
  automation that only wants the ring, not the panel).
- **`entity.py`** — shared base class. `_run_client_command` wraps every
  BLE call so a `BleakError` becomes a clean `HomeAssistantError` toast
  instead of a raw traceback in the UI. `device_info` is built fresh from
  the latest polled status each time (MAC, WiFi MAC, hardware version),
  which is what lets this integration's device correlate with other
  integrations (e.g. a network-monitoring integration that also knows the
  device's WiFi MAC) in HA's device registry.

### Device identity: name, MAC addresses, and serial number

Three things worth understanding clearly, since they look surprising at
first glance:

**The HA device name is already the BLE broadcast name, automatically.**
`config_flow.py` sets the config entry's title from `discovery_info.name`
(Bluetooth-discovery flow) or the equivalent value collected via
`bluetooth.async_discovered_service_info` (manual-entry flow) — both are
the device's actual over-the-air BLE local name, not something typed in
by hand. Confirmed directly against a live config entry: title
`GVH60A67457` for address `5C:E7:53:F4:74:57`, i.e. the model prefix plus
the last two MAC octets. This is **not** the friendly name you set in the
Govee app (e.g. "Kitchen Light") — that's confirmed cloud-only (§9 in
`PROTOCOL.md`), never broadcast over BLE or exposed through any `ab`
metadata field found so far. If you want a friendlier HA name, rename the
device in HA directly (Settings → Devices), or pull `deviceName` from
Govee's authenticated Cloud API (`GET /user/devices`, see `PROTOCOL.md`
§6.5 for how this project has queried that endpoint before) and set it by
hand — there's no way to get that specific string over BLE.

**Multiple MAC-like addresses per physical device is expected, not a
bug.** Each unit actually has (at least) three distinct address-shaped
identifiers, confirmed directly against the same real device:
- **BLE MAC** (`5C:E7:53:F4:74:57`) — what this integration connects to.
- **WiFi MAC** (`5C:E7:53:F4:74:56`) — reported via the device's own
  status query (`status.wifi_mac`), differs from the BLE MAC by exactly 1
  in the last octet.
- **Cloud API device ID** (`2D:DB:5C:E7:53:F4:74:56`) — an 8-byte value
  Govee's authenticated Cloud API uses to address the device, which
  itself embeds the WiFi MAC as its last 6 bytes plus a 2-byte prefix.

This is normal for combo WiFi+BLE chips, which commonly derive separate
per-radio MAC addresses from one base address with a small fixed offset,
since WiFi and Bluetooth are logically separate network interfaces even
on the same physical chip. `entity.py`'s `device_info` deliberately
registers **both** the BLE and WiFi MACs as `connections` on the same HA
device entry specifically so other integrations that discover this
device via a *different* interface (e.g. a network-scanning integration
that only ever sees the WiFi MAC) get correlated onto the same device in
HA's registry, rather than showing up as an unrelated duplicate.

**A real bug, found and fixed via this question**: if you ever see *more
than 2* MAC-shaped connections on the device page, that's not a third
legitimate address - it's stale garbage. `__init__.py`'s
`_sync_device_registry` re-syncs the registry on every successful poll
specifically so a bad value from one flaky read self-heals on the next
good one, but it originally did this via
`device_registry.async_get_or_create(connections=...)`, whose
`connections` argument only ever **adds** to the existing set - it never
removes anything. A bad connection written once (e.g. from an early
version of the WiFi-MAC parsing logic, or two lights' BLE traffic briefly
cross-contaminating at startup) therefore stuck around forever sitting
*alongside* the correct one, silently defeating the self-healing this was
meant to provide. Confirmed live: real device registry entries were found
carrying a garbled MAC (the correct WiFi MAC's bytes shifted by one
position with a stray `10` appended) that had persisted for months
alongside the correct one, invisible to every previous "self-heals on
next poll" assumption. Fixed by switching to
`device_registry.async_update_device(device_id, new_connections=...)`,
whose `new_connections` does a full replace instead of a merge - verified
live, the stale connection was gone after the very next successful poll,
no manual registry editing needed.

**The device serial number is available over BLE**, despite not being
somewhere obvious — `client.get_serial_number()` queries `ab` metadata
field `0x05` (see `PROTOCOL.md` §8) and is wired into `device_info` as
`serial_number`, fetched once at setup rather than on every poll since
it's static. Confirmed stable across two independently captured sessions
(identical value both times).

### Scene / effect activation

`light.py`'s `_activate_scene` prefers a **full data upload**
(`client.set_scene_full`) whenever the selected effect has real
`scenceParam` data available from the fetched scene library, falling back
to **bare activation** (`client.set_scene`, just the scene ID, no upload)
only for the static fallback table, which has no effect data to upload.
Full upload is slower (a multi-chunk BLE burst) but is guaranteed correct
regardless of whether the device has ever seen that scene before; bare
activation is fast but silently does nothing if the device hasn't cached
that exact scene from prior use. See `PROTOCOL.md` §6.4 for the full
history of why the default flipped from bare to full upload.

**Known-broken scenes are filtered out of the effect picker entirely**
(`const.BROKEN_SCENE_NAMES`, applied in `light.py`'s
`_sorted_selectable_scenes`) rather than left in as traps that silently
fail or visibly misbehave when selected. See "Known issues" below.

## Methodology

This integration's BLE protocol understanding was built entirely through
**live, controlled experimentation against real devices** — there was no
public documentation for the H60A6 specifically to start from (see
`PROTOCOL.md` §11 for the extensive prior-art search that confirmed this).
The general approach, repeated for each opcode/behavior:

1. **Capture real traffic.** Put a phone running the Govee app into a
   Bluetooth HCI snoop capture, perform the action in the app, pull the
   `btsnoop_hci.log`, and decode it against this project's own
   already-cracked encryption scheme.
2. **Form a hypothesis** about what the bytes mean, implement it, and
   write it up in `PROTOCOL.md` with the supporting evidence.
3. **Verify live, not just statically.** `test_protocol.py` is a
   standalone (no Home Assistant install required) unit test suite built
   from real captured fixtures — useful for regression-testing byte-level
   framing, but explicitly **not sufficient on its own**: several real
   bugs were only caught by literally connecting to a device and running
   every command (`test_live_device.py`, `test_scene_switching.py`, and
   similar one-off scripts), because status-query success and actual
   physical rendering are two different things that can disagree (see
   `PROTOCOL.md` §6.4/§6.5).
4. **Cross-check against Govee's own official Cloud API** when a live BLE
   result was ambiguous. With a real Govee Developer API key, several
   disputed scenes were activated through Govee's authenticated
   `POST /device/control` endpoint — completely bypassing this project's
   BLE code — to determine whether a rendering failure was a bug in *this
   integration* or a real limitation of the scene/device. This is how
   §6.3.1's two scene-upload failure modes were confirmed to be bugs in
   this project's BLE implementation, not the scene data (`PROTOCOL.md`
   §6.5).
5. **Search for prior art before assuming something is undiscovered.**
   Several community BLE reverse-engineering projects for *other* Govee
   devices were read in full to check for overlapping protocol details
   (chunking/checksum framing, opcode meanings) before concluding a given
   quirk is genuinely undocumented anywhere. `PROTOCOL.md` §11 records
   both what corroborated this project's own findings and what turned out
   to be dead ends, so future work doesn't repeat the same searches.

The standalone diagnostic/test scripts referenced above are not part of
the installed integration — they live outside `custom_components/` (see
the paths noted in `PROTOCOL.md` where each is discussed) and are meant to
be run by hand against a real device, not in CI.

## Known issues

See `PROTOCOL.md` §10 for the complete, currently-maintained list. The two
most relevant to day-to-day use:

### Broken scenes (hidden from the effect picker)

Two independently-confirmed root causes prevent some official scenes from
rendering correctly over BLE, even though they're genuinely valid,
currently-supported scenes for this device (confirmed via Govee's own
Cloud API — see `PROTOCOL.md` §6.5):

- **"`0xFF` placeholder" scenes** — Aurora, Dandelion, Desert, Fall,
  Green Wheat Field, Volcano. Their raw effect data contains literal
  `0xFF` bytes in header positions that look like unresolved template
  placeholders (in the same spirit as the already-solved `0x08`
  "unconfirmed template" flag bit, but a different field). Size-independent
  — happens even for the smallest of these scenes. **Additional testing
  needed:** a real BLE capture of the Govee app freshly uploading one of
  these scenes (device must not already have it cached) is the most
  direct path to the fix — everything tried so far without one has been
  inconclusive. See `PROTOCOL.md` §6.3.1 and §6.6 for the specific,
  structurally-motivated hypothesis (a per-scene-"type" prefix
  substitution, analogous to what's documented for *other* Govee device
  families) that a real capture would let us confirm or rule out.
- **Oversized scenes** — Ocean and Winter, the two largest scenes in the
  library. Ocean additionally causes an outright BLE disconnect, not just
  a silent failure to render. **Additional testing needed:** narrowing
  the actual size/chunk-count threshold (currently only bounded to
  "somewhere between ~254 and ~336 bytes") would need several
  differently-sized real scenes tested live, ideally with a real capture
  of the app uploading one of them fresh for comparison.

These are disabled in `const.BROKEN_SCENE_NAMES`, filtered out of the
effect picker in `light.py`, and additionally rejected with a clear error
if triggered directly (e.g. via a service call bypassing the dropdown).
Once either root cause is actually fixed and verified against a real
device, remove the corresponding names from that set.

### Zone on/off status readback is unreliable whenever the two zones differ

`PROTOCOL.md` §5.2/§5.2.1 documents an unresolved bug in how zone on/off
state is *decoded from a status query* (not a problem with the on/off
*command*, which is solid) — the byte-level encoding used for that field
doesn't fit the simple model this integration currently implements. It
can misreport any time the upper and lower zones simply differ from each
other, which is essentially any ordinary partial on/off state, not a
special case.

An optimistic-tracking mitigation was tried and then **reverted** — see
`PROTOCOL.md` §5.2.1 for the full account. Short version: it broke this
integration's ability to notice zone changes made outside HA (the Govee
app, a remote), and it turned out to be the wrong fix for the specific
report that prompted it anyway (see below). `light.py`/`switch.py` once
again trust the coordinator's polled zone state directly, with the known
readback bug intact and unresolved. See `PROTOCOL.md` §10 item 5 for what
real testing would be needed to actually fix the decode.

### "The light won't turn on/off" isn't always this bug — check for a stuck device first

A device can get wedged at the hardware/firmware level such that it
stops responding to power/zone commands entirely, while still responding
normally to brightness and color changes. This looks superficially
similar to the status-decode bug above but is a completely different,
unrelated problem — and no software fix in this integration can address
it, because the device isn't executing the command at all, on *any*
control path.

**How to tell the difference**: command power off through Govee's
official Cloud API directly (`POST /device/control` with
`devices.capabilities.on_off`/`powerSwitch`, requires a Govee Developer
API key), independent of this integration's BLE code entirely. If it
reports success but a follow-up `POST /device/state` query still shows
the device on, the device itself is wedged, not this integration. The fix
is a physical power cycle (the wall switch, or the breaker for a hardwired
fixture) — not a code change.

One related, separate gotcha observed after a power cycle: the device can
take a while to become visible to Home Assistant's Bluetooth stack again,
independent of whether it's already back online via WiFi/the Govee app.
Symptom in the log: `bleak_retry_connector.BleakOutOfConnectionSlotsError`
with `never seen by any scanner`. This was confirmed to be a real
Bluetooth-visibility gap, not a code bug — checked directly with
`bluetoothctl devices` / `bluetoothctl scan on` on the HA host, which also
didn't see the device. If this doesn't clear up within a few minutes on
its own, try moving the device closer to the host's Bluetooth adapter, or
restarting Home Assistant to force a fresh Bluetooth scan pass.

## File structure

```
__init__.py       Integration setup: creates the shared client/coordinator per
                   device, fetches the scene library, registers platforms.
client.py         BLE client: encryption/handshake, connection lifecycle,
                   all command builders, status query + parsing.
config_flow.py    Bluetooth-discovery and manual-address config flow.
const.py          Shared constants: UUIDs, PSK, zone IDs, the static scene
                   fallback table, and the broken-scenes denylist.
coordinator.py     DataUpdateCoordinator: periodic status polling.
entity.py          Shared base entity: error-wrapping helper, device_info.
light.py           The light entity: power/brightness/color/scene control.
scene_library.py   Fetches the live scene library from Govee's public API;
                   builds the a3-chunked scene upload payload.
switch.py          Per-zone (upper ring / lower panel) switch entities.
manifest.json      HA integration manifest.
PROTOCOL.md        The full reverse-engineered BLE protocol reference.
test_protocol.py   Standalone unit tests built from real captured fixtures
                   (no Home Assistant install required to run).
```
