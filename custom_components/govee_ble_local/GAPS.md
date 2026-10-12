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
- **H60A6 whole-fixture power via the global switch (`33 01`).** The status burst carries a global
  switch TLV separate from the two zones, so a single `33 01` frame may be able to switch the whole
  fixture. Unverified: every captured state had both off, and earlier live tests only drove zones.
  Whole-fixture power therefore keeps the proven per-zone frames (sent as one batch on one
  connection). Settle it with `tools/power_semantics_probe.py <addr> H60A6` from the library repo
  (needs a host in Bluetooth range), then switch `Device._power_frames` if `33 01` alone works.
- **`0xEE` notification pushes.** Parsed by the library but not consumed: none were observed in
  five days of captures (sessions are short and poll-driven).

## 3. Per-segment light entities — behavioural note

Segment lights (H60A6 / H6047 / H61A8 / H6641) model "off" as **setting the segment to black**
(there is no per-segment power line in Govee's protocol). Entity count is the static
`profile.segments` width; for IC-driven SKUs (e.g. H6641) the true group count is read live, so
some declared segment entities may never report state until read-back is confirmed (see §2).

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
(main = segment 12, background = 0–11, independently addressable, live-verified). The zone and
segment lights now expose `ColorMode.COLOR_TEMP` wherever the fixture supports kelvin.

*Resolved:* `Device.read_secret()` (config-flow secret auto-read) and
`Device.ingest_advertisement()` (passive on/off) were restored upstream and are used directly.
