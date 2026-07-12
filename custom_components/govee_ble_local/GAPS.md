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
- **H6047 / H6641 segment read-back — corrected upstream, still not live-verified.** The prior
  entry here (H6047 modeled as `readback="status"`, mechanism-A) was itself wrong and has been
  fixed in two steps on the library's `master` (not yet tagged past `v1.0.0`): neither SKU
  actually answers the `0xAC` status burst at all (source-confirmed: H6047 routes to
  `Compose4InfoBleIot`; H6641 goodsType 247 never reaches the `0xAC` dispatch) — both are back to
  `readback="polled"` (power/brightness/scene via `aa 01/04/05`), with per-segment colour read
  via a new **`color_readback="mechanism_a_direct"`** (direct per-group `aa a5 <group>` requests,
  correct group size 4 — an interim fix had reused H61A8's group-size-3 decoder, silently
  misassigning segment indices, since fixed). H6641 additionally now reads its true colour-group
  count live (`color_readback_live_ic`, a `0x40` IC-count query) instead of approximating from
  the write-mask width. This is a **real correctness fix** for exactly the gap this entry used to
  describe (the one live H6047 connect earlier returned an empty 0xAC) — but the library's own
  changelog doesn't claim hardware verification for `mechanism_a_direct` either, only that the
  test suite caught the wrong-decoder bug before it shipped. Once the library tags a stable
  release past `v1.0.0` and we bump the pin, this should be re-tested live on H6047 specifically.
- **H61A8 / H6052 segment/colour read-back.** Source-modeled (mechanism-B / -C respectively), no
  hardware on hand. H60A6 mechanism-A remains the verified reference.
- **H60A6 truncated/empty `0xAC` status bursts — fixed upstream (retry), matches our own live
  test.** Separately from the segment-mechanism fix above, the library's `master` now retries a
  status read once on an empty parse before leaving state stale (rather than giving up
  immediately), specifically to recover a single dropped BLE notification mid-burst. We
  independently live-tested this exact fix on `core` before it was merged (H60A6 recovered full
  state — brightness, all 13 segments, both zones — on retry in ~1.3s where it previously logged
  `state left stale` on ~80-88% of polls); the library's own changelog reports the same result
  field-tested on their end. Not yet pinned (see the version note below).


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
