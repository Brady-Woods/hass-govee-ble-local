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
- **H6047 segment read-back.** H6047 moved to `readback="status"` (mechanism-A) and the
  integration exposes per-segment lights. **Caveat:** the one live H6047 connect returned an
  **empty 0xAC**, so mechanism-A on H6047 is unconfirmed — segment colours may not populate.
- **H61A8 / H6052 / H6641 segment/colour read-back.** Source-modeled (mechanism-B / -C /
  shared-A), no hardware on hand. H60A6 mechanism-A is the verified reference.
- **Device-info read-back (`serial` / `wifi_mac` / `hw` / `fw`) and `ble_mac`.** Library now
  reads aa 07 (basic/wifi/SN) once into `DeviceState`; the integration surfaces `wifi_mac`/
  `ble_mac` as registry connections and hw/fw/serial as device-info. **Observed returning
  all-zeros** on live hardware (MAC `00:00:00:00:00:00`, versions/serial `0`) — the aa 07
  parse yields zeroed fields for at least some SKUs. The integration now defensively drops
  all-zero MAC / `0`-ish version+serial (`helpers.clean_mac`/`clean_text`) so it shows nothing
  rather than garbage; the underlying parse should return `None` for zeroed fields (library
  fix) and the layout still needs a clean capture to verify.


## 3. Per-segment light entities — behavioural note

Segment lights (H60A6 / H6047 / H61A8 / H6641) model "off" as **setting the segment to black**
(there is no per-segment power line in Govee's protocol). Entity count is the static
`profile.segments` width; for IC-driven SKUs (e.g. H6641) the true group count is read live, so
some declared segment entities may never report state until read-back is confirmed (see §2).

---

*Resolved:* Per-zone/per-segment **colour temperature** — the library added
`set_zone_color_temp` / `set_segment_color_temp` (masked CCT) and corrected the H60A6 topology
(main = segment 12, background = 0–11, independently addressable, live-verified). The zone and
segment lights now expose `ColorMode.COLOR_TEMP` wherever the fixture supports kelvin.

*Resolved:* `Device.read_secret()` (config-flow secret auto-read) and
`Device.ingest_advertisement()` (passive on/off) were restored upstream and are used directly.
