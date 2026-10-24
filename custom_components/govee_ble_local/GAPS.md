# Known gaps & pending verification

Status of features that depend on the underlying
[`govee-ble-local`](https://github.com/Brady-Woods/govee-ble-local) library (v3 API). The
integration is a plugin-only consumer; library work is tracked upstream.

## 1. H5122 button sensor — not supported locally (deferred)

The library spec (`spec/devices.yaml`, family `h512x`, ~line 1087) documents the button/contact
sensor family, including **H5122 "Button Sensor"** (goodsType 131). It is **not implemented**:

- Button SKUs (H5122 / H5125 / H5126) deliver events via **cloud push (WarnMessage), not local
  BLE**, per Govee's own app; there is no confirmed local press-event delivery path.
- The family's advertised BLE `name_prefixes` are flagged **UNVERIFIED** (no broadcast parser
  in the decompiled source).
- H5122 is absent from the runtime `PROFILES` table with no button `Capability`.

Needs a real btsnoop capture / verified advertisement format, then library work (a `Capability`,
profile entry, advertisement event parser) plus a Home Assistant `event` platform here.

## 2. Wired but NOT live-verified

These are implemented end-to-end (library + this integration) but not yet confirmed on hardware.
Treat their read-back values as provisional.

- **Plug power poll (H5083 family).** Library reads the relay bitmask (aa 01) into
  `state.is_on`; the HA power switch now reflects it. **Unverified** — needs an H5083 to confirm
  the query (aa 01 vs plug-spec) and the relay-bit → on/off mapping.
- **H6047 segment read-back — live-verified (2026-10-12).** The direct per-group `aa a5 <group>`
  read (`mechanism_a_direct`, group size 4) returns all 12 read-back pieces on a real H6047, and the
  bar power read (`aa 36`, polled since library 1.1.0) and colour temperature read-back work too.
  **H6641** uses the same decoder plus a live `0x40` IC-count read; still no H6641 hardware.
- **H61A8 / H6052 segment/colour read-back.** Source-modeled (mechanism-B / -C respectively), no
  hardware on hand. H60A6 mechanism-A remains the verified reference.
- **H60A6 truncated `0xAC` status bursts — root-caused and fixed (library 1.1.0), live-verified.**
  The lost terminator on the first query of a session was a handshake race (the query fired while
  the device's `e7 02` confirm was still in flight). With the handshake fixed, the first burst
  arrives complete; a lost tail now decodes to partial-but-true state without a retry.
- **H60A6 whole-fixture power — resolved (2026-10-12, live probe on hardware).** The global `33 01`
  switch drives both zones (off clears them; on lights both even when each was off), so
  whole-fixture on/off is now a single frame. Colour and brightness writes also wake the fixture.
- **`0xEE` notification pushes.** Parsed by the library but not consumed. Only pushed to a
  connected client, and connections are short-lived by design (connection slots are scarce), so
  they're rare: none in five days of captures; one `ee 30 01 01` (zone flags) seen during a probe.
  External changes are picked up by the advertisement on/off byte and the background polls
  instead (see the README's Polling section).

## 3. Per-segment light entities — behavioural note

Segment lights (H60A6 / H6047 / H61A8 / H6641) model "off" as **setting the segment to black**
(there is no per-segment power line in Govee's protocol). A segment shows off while its fixture is
known off. On an H60A6 or H6047 that isn't known on, a segment's turn_off sends nothing: there a
`33 05` colour frame switches the fixture on and lights every zone. The library's firmware
analysis predicts that wake (`Device._wake_power`, govee-ble-local 1.5.0), and a bench check of one
of each confirmed it on 2026-10-21. A black segment frame was sent 0.05 s or 1 s after the
fixture's acked `33 01 00`, as one frame or one per segment, with or without the zone-offs between.
Every time, the fixture came back on, both zones lit, read back 2 s and 12 s later. The H60A6
pushed `ee 30` with every zone on about 45 ms after the frame; the H6047 answered `aa 01 01` and
`aa 36 01 01`. With no segment frame it stayed off.

On a fixture known on, a segment also shows off while its zone is known off (an H60A6's main panel
or ring, an H6047 bar), and its turn_on switches that zone on first. That rests on a premise
**modelled from the firmware, not checked on the bench**: with the fixture on, a `33 05` lights no
zone (`Device._wake_power` models the wake only while the fixture isn't known on). A zone whose
power is unknown or held on gets no zone power from a segment: on an H6047 a bar switch reads
both bars first (`aa 36`, which can go unanswered) while it doesn't know them, or knows the other
bar only from long ago, and its `33 36` handler re-runs the power-on tail, which may restart a
running scene or mode.

Entity count: the segment count the device last reported, once the library stamps a count as read
from the device (`helpers.confirmed_segment_count`) and two reads in a row give it. A read is one
the device answered with a count (a new stamp); a poll that failed, or whose count read went
unanswered, is none. A count that holds for two answered reads is taken, and reloads the entry, at
each change, with no limit; one that changes from one answered read to the next removes and reloads
nothing. It is stored in the entry, so a restart starts from it. Otherwise it is the library's
`segment_count` (the profile's until the device reports one). A reported count removes the segment
lights past it, and a larger one reloads the entry to bring them back with their registry settings
(Home Assistant keeps a removed entity's record while its config entry exists, and purges it 30 days
after the entry is deleted). A count the device hasn't reported removes none. On a library without
the stamp (1.6.1) the count is the profile's, as before.

---

*Resolved:* **Device-info for BLE-only devices (H60A6)** — the library re-added the
`0xAC`-status-anchored extraction (`reassemble.anchor_device_info`, byte-exact from v2) plus
zero-gating in `parse_device_info`, so `readback="status"` devices repopulate
`state.wifi_mac` + `hardware_version` from the status burst (their only source). The HA panel's
Wi-Fi-MAC / hardware version return; the integration's `clean_mac`/`clean_text` guard is now
belt-and-suspenders rather than the thing hiding data. (Firmware/serial still only where the
`aa 07` queries answer non-zero.)

*Resolved:* Per-zone/per-segment **colour temperature** — the library added
`set_zone_color_temp` / `set_segment_color_temp` (masked CCT) and corrected the H60A6 topology
(main = segment 12, background = 0–11, independently addressable, live-verified). A zone light
exposes `ColorMode.COLOR_TEMP` wherever the fixture supports kelvin and the zone has segments (a
zone without segments is an on/off light). A segment light exposes it only where the library says
the model's segments take one (`segment_color_temp`); elsewhere Home Assistant sends a colour
temperature asked of a segment as an RGB colour.

*Resolved:* `Device.read_secret()` (config-flow secret auto-read) and
`Device.ingest_advertisement()` (passive on/off) were restored upstream and are used directly.
