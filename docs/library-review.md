# Library-dependent review items

This document lists the problems found in the review of hass-govee-ble-local whose fix depends on the
govee-ble-local library. Each one needs a library change, a new library API, a hardware check of
library behaviour, or an integration change that should be written against the refactored library.
The library is in the middle of a refactor, so none of these are fixed in the next release. Review
them after that release is cut. The integration-side fixes wait for the refactored library API.

**Versions referenced**

| Name | What it is |
|---|---|
| integration `dc1c87f` | The hass-govee-ble-local commit that was reviewed: the `master` merge of PR #14, manifest version 1.4.1, requirement `govee-ble-local>=1.2.1,<2`. Integration line numbers are at this commit. The working tree is being changed for the next release, so some lines in `__init__.py`, `coordinator.py`, `entity.py`, `services.py`, `button.py` and `tests/` will move. References marked *(working tree)* point at that next-release tree (branch `fix/ha-code-standards`, uncommitted) instead. |
| `1.2.1` | Library tag `v1.2.1`, the manifest floor. Users can stay on it indefinitely (CLAUDE.md rule 2). |
| `1.3.0` | Library tag `v1.3.0` (tagged 2026-10-15). It is not on PyPI yet (PyPI has 1.2.0 and 1.2.1, checked 2026-10-17), so the `LIB_VERSION=newest` CI leg currently resolves to 1.2.1 too; "newest leg" below means that leg once 1.3.0 or later is published. Cited only where it differs from 1.2.1. |
| `dev` | Library checkout at `c823fc5` (61 commits after `v1.3.0`, the `[Unreleased]` CHANGELOG section), mid-refactor. |

Library paths are relative to `src/govee_ble_local/` in the library repository, for example
`1.2.1 devices/device.py:587` or `dev devices/_fold.py:36`.

**How to use this document**

For each item:

1. Decide the library side: accept the proposed change or API, change it, or reject it. Record the
   result on the item's **Decision** line. Items marked *hardware check* need that check first,
   because its result can change the decision.
2. Once a library release carries the change, make the integration change and add the listed test.
   Raise the manifest floor only when the integration starts using something the new release adds,
   or guard the use (for example with `getattr`) so the floor can stay (CLAUDE.md rules 1-3). CI
   tests both the floor and the newest release; both must pass.

**Status words used below**

- *still present in dev @ c823fc5*: the dev checkout behaves as 1.2.1 does for this item.
- *changed in dev: ...*: the dev checkout already changes the behaviour, as described.
- *re-check after the refactor*: the item depends on code the refactor is likely to change.

**Severity** is the final severity from the verified finding. Review notes that were never turned
into a finding have no severity and are marked *unrated*. "Confirmed by N verifiers" counts the
verifier votes recorded for the finding.

## Re-check at library 4f2c073 (2026-10-17)

Every item below was re-checked against the refactored library, `fix/review-findings` @ `4f2c073`
(Kaitai spec refactor merged, public API unchanged, not yet released; the wheel reports 1.3.0). Each
"fixed" or "changed" verdict was confirmed by a second, independent check. Totals: 28 still open,
3 fixed (none released yet), 2 changed by the refactor, 2 not library-dependent, 1 needs
hardware.

"Next step" says who moves first. *Integration (floor)*: the integration can make the change now
with APIs that exist at the 1.2.1 floor (guard anything newer with `getattr`). *Library*: it waits
for a library change. Item details further down still describe the 1.2.1 / `c823fc5` state.

| # | Status at 4f2c073 | What changed | Next step |
|---|---|---|---|
| 1.1 | still open | Unchanged. An H60A6 on pact (1, 1) no longer gets 0xA5 groups, so its snapshot has no segments; out-of-range segment indices now raise `GoveeBleInvalidValue` | Integration (floor): per-segment snapshot/restore, filtered to `segment_count` |
| 1.2 | still open | Setters still never power on; the H60A6 wake result is only a spec note | Integration (floor): power on first, then colour, power last |
| 1.3 | changed by the refactor | From 1.3.0 a segment RGB that is exactly a Kelvin tint goes out as a CCT frame, so the H60A6 repaint keeps CT mode; the floor still repaints RGB | Integration (floor): keep the skip-RGB-after-CCT guard for 1.2.1 |
| 1.4 | still open | Unchanged | Integration (floor): restore by scene name |
| 1.5 | still open | Unchanged; H6047 now has the gradual flag too | Integration (floor): guard on the snapshot |
| 1.6 | still open | Unchanged (plug power rules changed around it) | Integration (floor): skip relay power-on when the start state is unknown |
| 2.1 | still open | Unchanged: `update()` can still succeed silently after a failed read | Integration (floor) for the raised-error part; Library for a public read outcome |
| 2.2 | still open | New handshake exception classes, but link-down vs per-command failure is still indistinguishable | Library: link-down marker and a no-ack split |
| 2.3 | changed by the refactor | On H60A6 the extra read is now a single 0xAC query | Integration (floor): decide whether to skip it |
| 3.1 | not library-dependent | Only message text changed | Integration: typed report |
| 3.2 | still open | Field names unchanged | Integration: typed accessors |
| 3.3 | not library-dependent | Unchanged | Integration: stable `reason` code |
| 4.1 | still open | Unchanged | Integration (floor): fixture-off gate plus power-on |
| 4.2 | still open | No `Segment.kelvin`; per-bar Kelvin is still H6047-only | Integration (floor) stopgap; Library for `Segment.kelvin` |
| 4.3 | still open | Unchanged | Integration (floor): remember the Kelvin across off |
| 4.4 | still open | Unchanged | Library: fold whole-device writes into `state.segments` |
| 4.5 | still open | An off advertisement now fills `zone_power`, so the restart case is gone; zone-on still doesn't set `is_on` | Library: set `is_on` on zone-on |
| 4.6 | fixed (unreleased) | An off advertisement marks every zone off | None; add the newest-leg test once released |
| 4.7 | still open | Reachable differently now (see 4.5) | Integration: add the test |
| 4.8 | still open | Unchanged since 1.3.0 | Integration (floor): index filter with 1.1 |
| 5.1 | still open | Setters moved onto a shared commit helper; cost unchanged | Integration (floor): skip the known-on zone power write |
| 5.2 | needs hardware | Nothing settles it | Hardware check on H60A6 and H6047 (section 10) |
| 5.3 | still open | No batched zone/segment write | Library: batched API |
| 6.1 | still open | Polls still hold the link 8 s; `idle_disconnect` works at both ends | Integration (floor): short `idle_disconnect` |
| 6.2 | still open | No public non-terminal disconnect | Library: `Device.disconnect()` |
| 6.3 | still open | `close()` still waits up to 45 s behind an in-flight operation | Library: cancel-aware close |
| 6.4 | still open | `stop()` unchanged | Integration (floor): HA-stop listener |
| 7.1 | still open | The builder raises `GoveeBleNotSupported` itself; no capability flag | Integration (floor): cached fallback; Library for a flag |
| 7.2 | still open | Unchanged; 15 segments now enforced | Integration: add the test |
| 7.3 | still open | `segment_count` stays 16 on H6641; still undecided | Integration (floor) via `getattr`; Library decision |
| 8.1 | still open | Parser hardening only | Integration: realistic fixtures |
| 8.2 | still open | Documentation only | Integration (optional): `dev.ble_mac or ""` |
| 8.3 | fixed (unreleased) | `GoveeCloudAuthError` exists | Integration (floor): guarded import |
| 9.1 | fixed (unreleased) | Index no longer rebuilt per call | None |
| 9.2 | still open | Whole-device read-back is clamped; segment read-back isn't | None (the integration already clamps) |
| 9.3 | still open | The parse debug log still prints secret-carrying frames unmasked | Library: mask them |

## Summary

| # | Id | Area | Severity | Needs | Status in dev |
|---|---|---|---|---|---|
| 1.1 | `restore-drops-per-segment-colours` | Self-test snapshot & restore | medium | integration change after refactor; hardware check (H6047) | still present |
| 1.2 | `restore-colour-sent-to-unlit-fixture` | Self-test snapshot & restore | medium | hardware check; library change (document power semantics); integration change | still present |
| 1.3 | `cct-restore-overwritten-by-rgb-repaint` | Self-test snapshot & restore | medium | hardware check (H60A6); integration change; library API addition (see 4.2) | still present; re-check after the refactor |
| 1.4 | `scene-restore-without-upload` | Self-test snapshot & restore | medium | integration change after refactor | still present |
| 1.5 | `unknown-original-state-not-guarded` | Self-test snapshot & restore | low | integration change; hardware check (H61A8) | still present; dev adds the gradual flag to H6047 |
| 1.6 | `unknown-power-relay-left-on` | Self-test snapshot & restore | low | integration change; hardware check (H5083) | still present |
| 2.1 | `readback-ok-checks-optimistic-echo` | Self-test verification & abort | medium | library API addition; integration change | still present (read outcomes tracked internally, not exposed) |
| 2.2 | `self-test-no-abort-on-dead-link` (remainder) | Self-test verification & abort | medium | library API addition (exception taxonomy); integration change | partly changed: new handshake and command-rejected classes; link-down vs per-command still not distinguishable; connect error messages reworded |
| 2.3 | `readback-without-verify` | Self-test verification & abort | nit | integration change after refactor | changed in dev: the H60A6 poll is one status burst |
| 3.1 | `self-test-report-untyped` | Self-test report & service shape | low | integration change after refactor | no library dependency |
| 3.2 | `stringly-typed-verify-helpers` | Self-test report & service shape | nit | integration change after refactor | field names unchanged in dev |
| 3.3 | `capture-response-mixed-error-format` | Self-test report & service shape | nit | integration change | no library dependency |
| 4.1 | `segment-light-ignores-fixture-power` | Sub-light state model | medium | integration change; optional library API addition (5.3) | still present |
| 4.2 | `sublight-cct-mode-never-reconciles` | Sub-light state model | medium | library API addition (per-segment kelvin); hardware check; integration change | partly changed: per-bar kelvin for H6047 only |
| 4.3 | `segment-cct-lost-after-off` | Sub-light state model | low | library API addition (4.2); integration change | still present |
| 4.4 | `whole-device-colour-not-folded-into-segments` | Sub-light state model | unrated | library change | still present |
| 4.5 | `zone-light-off-after-turn-on-when-fixture-off` | Sub-light state model | low | library change | changed in dev for the advertisement path |
| 4.6 | `zone-power-stale-after-advert-off` | Sub-light state model | unrated | library change | changed in dev |
| 4.7 | `zone-on-from-off-untested` | Sub-light state model | low | integration change (test) after the library release | follows 4.5 |
| 4.8 | `segment-index-space` | Sub-light state model | unrated (finding refuted) | library change (document); hardware check (H6047) | changed in 1.3.0 and dev |
| 5.1 | `zone-light-redundant-power-write` | Zone command batching & ordering | low | integration change; library API addition (5.3) | still present |
| 5.2 | `zone-power-sent-after-colour` | Zone command batching & ordering | low (uncertain) | hardware check (H60A6, H6047) | still present |
| 5.3 | `batched-sub-light-writes` | Zone command batching & ordering | unrated | library API addition | still absent |
| 6.1 | `poll-semaphore-released-while-link-held` | Connection & slot usage | low | integration change (possible at the floor); re-check after refactor | still present |
| 6.2 | `no-nonterminal-disconnect` | Connection & slot usage | unrated | library API addition | still absent |
| 6.3 | `close-waits-for-inflight-op` | Connection & slot usage | unrated | library change | still present |
| 6.4 | `no-ha-stop-disconnect` | Connection & slot usage | unrated | library change (document the `Device.stop()` contract; 6.3); integration change | still present (no library change yet; integration has no stop listener) |
| 7.1 | `h61a8-segment-brightness-always-fails` | Capability / feature flags | medium | library API addition; integration change | still present (left to the builder by design) |
| 7.2 | `h61a8-segment-lights-untested` | Capability / feature flags | medium | integration change (test) | follows 7.1 |
| 7.3 | `runtime-segment-count` | Capability / feature flags | unrated | integration change after refactor; library decision; hardware check (H6641) | changed in 1.3.0: `Device.segment_count` exists |
| 8.1 | `cloud-device-id-fixture-unrealistic` | Cloud parsing | low | optional library API addition; integration change (test) | parser still private; docs improved |
| 8.2 | `cloud-ble-mac-may-be-wifi-mac` | Cloud parsing | unrated (finding refuted) | none unless evidence appears | changed in dev: documented as best effort |
| 8.3 | `cloud-auth-vs-network-error` | Cloud parsing | unrated | integration change after the library release | changed in dev: `GoveeCloudAuthError` |
| 9.1 | `active-scene-reverse-index-rebuilt` | Library-internal bugs & performance | unrated (negligible) | library change | changed in dev: cached |
| 9.2 | `segment-brightness-readback-unclamped` | Library-internal bugs & performance | unrated | library change (optional: the integration clamp is done) | still present for segments |
| 9.3 | `parse-debug-log-unmasked-secret` | Library-internal bugs & performance | unrated | library change | still present |

Section 10 collects the hardware checks.

## 1. Self-test snapshot & restore

All six items are in `capture.py`, which the self-test button ([custom_components/govee_ble_local/button.py:49-61](../custom_components/govee_ble_local/button.py#L49-L61)) and the
admin-only `capture_session` service ([custom_components/govee_ble_local/services.py:94-103](../custom_components/govee_ble_local/services.py#L94-L103)) both run. The module promises to
capture the full pre-test state and restore it afterwards. `restored` in the report only means that
no restore action raised ([custom_components/govee_ble_local/capture.py:592](../custom_components/govee_ble_local/capture.py#L592)).

### 1.1 `restore-drops-per-segment-colours`

- **Severity / source:** medium (raised as high). Review finding.
- **Symptom in Home Assistant:** after a self-test, segmented lights do not get their per-segment
  colours back, and the report still says `restored: true`.
  - H61A8 and H6641 (segments, no zones), after a Home Assistant restart or after the colour was set
    in the Govee app: only segment 0 is restored. The other segments keep the test state: the test
    RGB on H61A8; on H6641 the mid-kelvin white from the later `color_temp` step.
  - H60A6: the 12-segment background ring is repainted with segment 0's colour.
  - H6047: each bar is repainted with its first segment's colour.
  - Code: the snapshot keeps segment 0 and one colour per zone ([custom_components/govee_ble_local/capture.py:229-230](../custom_components/govee_ble_local/capture.py#L229-L230),
    [custom_components/govee_ble_local/capture.py:240-253](../custom_components/govee_ble_local/capture.py#L240-L253), `_zone_colour` at [custom_components/govee_ble_local/capture.py:204-211](../custom_components/govee_ble_local/capture.py#L204-L211)); the restore puts back the
    whole-device colour, segment 0, then one colour per zone ([custom_components/govee_ble_local/capture.py:280-295](../custom_components/govee_ble_local/capture.py#L280-L295)). The test
    [tests/test_capture.py:436-462](../tests/test_capture.py#L436-L462) asserts the lossy behaviour (background gets segment 0's
    colour).
- **Library behaviour:**
  - The test repaints every segment: whole-device `set_rgb` / `set_color_temp` use the all-segments
    mask (`1.2.1 wire/build.py:121-141`).
  - The whole-device `rgb_color` is only read back from an `aa 05 0d` mode report
    (`1.2.1 devices/device.py:689-700`, `1.2.1 wire/parse.py:145-156`). H60A6, H6047, H61A8 and H6641
    do not send one for their own writes, so `rgb_color` is only the echo of the last setter in this
    process, and None after a restart.
  - The read-back does carry every segment: the H60A6 status TLVs (`1.2.1 wire/reassemble.py:143-151`),
    H61A8 mechanism B (`1.2.1 devices/device.py:755-774`), H6047 / H6641 mechanism A direct
    (`1.2.1 devices/device.py:776-812`).
  - Still present in dev @ c823fc5. The whole-device RGB is still not decoded on these SKUs:
    `mode_color_parse` is `"kelvin"` (H61A8), `"kelvin_15"` (H6047, H6641) and
    `"kelvin_15_rgb_kelvin_0d"` (H60A6: RGB only from a 0x0d report) (`dev devices/profile.py:328`,
    `:372`, `:395`, `:477`; `dev devices/_fold.py:214-298`). Two dev changes matter here:
    - An H60A6 that has advertised a pact other than (1, ≥2) no longer asks for colour groups in the
      status burst, so its snapshot has no segments at all (`dev devices/profile.py:329-333`,
      `dev devices/device.py:1114-1121`).
    - `Device.snapshot()` returns a deep copy of the state (`dev devices/device.py:291-294`).
  - Index space: 1.2.1 reads 12 pieces back on H6047 but addresses 10 (`1.2.1 devices/profile.py:117`),
    and `_mask` accepts any index below 16 (`1.2.1 devices/device.py:53-60`). See 4.8.
- **Proposed library change:** none required for the fix. `Device.snapshot()` (already in dev) is the
  natural way to take the pre-test copy. Item 4.8 asks the library to document the index space of
  `state.segments`.
- **Integration change and test:**
  - Snapshot the whole segment list (`list(state.segments)`; `Segment` is frozen), filtered to
    `index < device.profile.segments` while the floor is 1.2.1.
  - Replace the segment-0 and per-zone restores with a per-segment restore. Do not add it beside them:
    the zone loop runs last and would repaint the H60A6 ring again. Group indices by colour and,
    separately, by brightness, and send one `set_segment_rgb` / `set_segment_brightness` per group.
    Keep `set_zone_rgb` only as a fallback for a zone none of whose segments were reported.
  - Skip the per-segment RGB repaint when a whole-device colour temperature was captured (1.3).
  - Tests: update `test_restore_static_colours_segments_and_zones`; add H61A8 and H6641 cases that
    prime several distinct segment colours with `rgb_color=None` and assert each segment is restored;
    add an H60A6 multi-colour background case.
- **Verification:** confirmed by 3 verifiers (code path, framework, impact); all three lowered it to
  medium (a deliberate diagnostic run, recoverable). One correction: on H6641 the leftover segments
  show the mid-kelvin white, not (0, 128, 255). Hardware check for the H6047 index filter: see 4.8.
- **Decision:** open

### 1.2 `restore-colour-sent-to-unlit-fixture`

- **Severity / source:** medium (raised as high). Review finding.
- **Symptom in Home Assistant:** when the starting power is known, the test ends with every zone off
  ([custom_components/govee_ble_local/capture.py:488-492](../custom_components/govee_ble_local/capture.py#L488-L492)) and then a whole-device power off ([custom_components/govee_ble_local/capture.py:507-514](../custom_components/govee_ble_local/capture.py#L507-L514)). The
  restore sends the scene, colour, segment, zone colour, brightness and gradual frames first
  ([custom_components/govee_ble_local/capture.py:325-329](../custom_components/govee_ble_local/capture.py#L325-L329)) and power last ([custom_components/govee_ble_local/capture.py:330](../custom_components/govee_ble_local/capture.py#L330), [custom_components/govee_ble_local/capture.py:298-314](../custom_components/govee_ble_local/capture.py#L298-L314)). If
  the firmware acks colour or brightness while off and then drops it, a light that was on comes back
  showing the test values (blue, green zones, red segment 0, 60 % brightness), and the report says
  `restored: true`.
- **Library behaviour:**
  - The colour and brightness setters send one frame each and never power the device on
    (`1.2.1 devices/device.py:377-442`, `:553-584`). Only `set_scene` folds power on (`:595`).
  - `Device.turn_on` says "power first so the rest lands on a lit fixture"
    (`1.2.1 devices/device.py:335-338`, `dev devices/device.py:619-622`).
  - Still present in dev @ c823fc5: the scene fold powers on, the RGB and kelvin folds do not
    (`dev devices/_fold.py:62-91`).
  - The library does not document what a colour or brightness write does to a device (or zone) that
    is off. Its probe `tools/power_semantics_probe.py` asks exactly that for the whole device
    (steps 5-6, lines 121-128). The verifiers found no recorded result in the library docs. The
    integration's own [custom_components/govee_ble_local/GAPS.md:39-41](../custom_components/govee_ble_local/GAPS.md#L39-L41) (commit `e862177`) records, from a 2026-10-12 live probe on
    an H60A6, that "colour and brightness writes also wake the fixture". That covers the H60A6 whole
    fixture only, not zones or other SKUs; the verifiers did not cite it.
- **Proposed library change:** once measured, document per profile what a colour, brightness,
  segment or zone-colour write does to an off device and to an off zone: dropped, stored, or wakes it.
  Suggestion (not from the review): a profile field such as `DeviceProfile.colour_wakes: bool | None`
  (None = unknown) so callers can order frames without guessing.
- **Integration change and test:**
  - Power the fixture on first whenever POWER is present, the starting `is_on` is known and the
    device has a colour capability. Then restore colour, brightness and gradual, then the zone
    differences, then send `set_power(False)` last only if the device was off. Do not send
    `set_power(True)` a second time at the end: on a zoned fixture it re-lights every zone.
  - When whole-device power is unknown, switch on the zones that were known on before restoring
    colour.
  - Tests: the order assertions in [tests/test_capture.py:424-429](../tests/test_capture.py#L424-L429), [tests/test_capture.py:454-462](../tests/test_capture.py#L454-L462),
    [tests/test_capture.py:494-497](../tests/test_capture.py#L494-L497) and [tests/test_capture.py:522-525](../tests/test_capture.py#L522-L525); the call-number failure
    injection in [tests/test_capture.py:532-533](../tests/test_capture.py#L532-L533) and [tests/test_capture.py:560-561](../tests/test_capture.py#L560-L561); the plug
    frame counts in [tests/test_button.py:84](../tests/test_button.py#L84) and [tests/test_button.py:121-128](../tests/test_button.py#L121-L128) (a plug has no
    colour capability, so it keeps three frames).
- **Verification:** confirmed by 2 verifiers, uncertain for 1 (impact lens: the visible failure
  depends on firmware; the everyday zone light already sends colour before zone power and has been in
  use on an H60A6 without a reported failure). Needs hardware: H60A6 zones and H6047, see section 10.
- **Decision:** open

### 1.3 `cct-restore-overwritten-by-rgb-repaint`

- **Severity / source:** medium. Review finding (also review note `oos.performance.capture.0`).
- **Symptom in Home Assistant:** an H60A6 left in colour-temperature mode is restored to that kelvin
  and then repainted as RGB: segment 0, the main zone (segment 12) and the background zone (0-11),
  which is every segment ([custom_components/govee_ble_local/capture.py:280-295](../custom_components/govee_ble_local/capture.py#L280-L295)). The fixture ends in RGB (for example RGB white
  instead of 2700 K) with the background flattened to segment 0's colour. Home Assistant keeps
  showing `color_temp` ([custom_components/govee_ble_local/light.py:199-204](../custom_components/govee_ble_local/light.py#L199-L204)), because the zone and segment RGB setters leave
  `color_temp_kelvin` set, so the state no longer matches the light. The test
  [tests/test_capture.py:436-462](../tests/test_capture.py#L436-L462) locks in this sequence.
- **Library behaviour:**
  - The H60A6 status burst returns an RGB record for every segment whatever the colour mode
    (`1.2.1 wire/reassemble.py:143-151`), merged into state (`1.2.1 devices/device.py:850-853`).
  - `set_color_temp` does not clear `state.segments` (`1.2.1 devices/device.py:404-417`;
    `dev devices/_fold.py:76-82`).
  - The segment RGB frame carries kelvin 0; the segment CCT frame carries FF FF FF plus the kelvin
    (`1.2.1 wire/build.py:144-167`). Each later segment or zone RGB write puts those segments back in
    RGB mode.
  - Still present in dev @ c823fc5 for segments. Dev adds a whole-device `last_color_temp_kelvin`
    (`dev models.py:97-103`). Re-check after the refactor: on dev, `set_segment_rgb` /
    `set_zone_rgb` send a colour that equals one of the app's Kelvin tint colours as a CCT frame
    (library `README.md:104-110`), and every CCT frame now carries the tint (`README.md:100-102`).
    If hardware shows a CT segment reads back as its tint, the per-segment restore of 1.1 would put
    CT back on dev without special handling.
  - Neither version documents what a segment in CT mode reports in the status burst (black, the
    tint, or the last RGB).
- **Proposed library change:** document what each read-back mechanism reports for a segment in CT
  mode, and add per-segment colour mode to the state (`Segment.kelvin`, item 4.2) so a snapshot can
  tell CT segments from RGB ones.
- **Integration change and test:** skip only the RGB repaints when a colour temperature was restored,
  and still restore segment brightness (op 0x02 does not change the colour mode). Update
  [tests/test_capture.py:436-462](../tests/test_capture.py#L436-L462) to expect `[set_color_temp(3000), set_segment_brightness([0], 33),
  set_brightness(70), set_power(True)]`, and add an RGB-mode variant that still expects the segment
  and zone repaints. Do this together with 1.1 and 1.2, which change the same function.
- **Verification:** confirmed by 1 verifier, with one correction: Home Assistant does not switch to
  reporting `rgb`; it keeps reporting `color_temp` while the device is physically in RGB. Needs
  hardware: an H60A6 left in CT mode.
- **Decision:** open

### 1.4 `scene-restore-without-upload`

- **Severity / source:** medium. Review finding.
- **Symptom in Home Assistant:** the test runs the first three scenes by name
  ([custom_components/govee_ble_local/capture.py:436-442](../custom_components/govee_ble_local/capture.py#L436-L442)), which uploads each scene's effect before activating it. The restore
  re-activates the original scene by code only ([custom_components/govee_ble_local/capture.py:276-279](../custom_components/govee_ble_local/capture.py#L276-L279)). The device keeps the last
  test scene's uploaded effect, and the report says `restored: true`. The light entity itself only
  uses `set_scene_by_name` ([custom_components/govee_ble_local/light.py:271](../custom_components/govee_ble_local/light.py#L271)). The test [tests/test_capture.py:480-498](../tests/test_capture.py#L480-L498) pins the
  code-only restore.
- **Library behaviour:**
  - `set_scene(code)` is "Activate a scene by its BLE code (no upload)" (`1.2.1 devices/device.py:587-599`);
    `set_scene_by_name` uploads the a3/a4 frames when the scene needs them, then activates
    (`1.2.1 devices/device.py:606-617`).
  - The verifier ran 1.2.1's `scene_upload_frames` over the catalogues: upload scenes are 73 of 84 on
    H60A6, 57 of 65 on H6047, 136 of 148 on H61A8 and H6641, 55 of 59 on H6006 / H6008, 6 of 43 on
    H6052. The first three sorted names need an upload on every SKU except H6052.
  - Still present in dev @ c823fc5: same split (`dev devices/device.py:919-925`, `:940-966`), now
    under `_scene_lock` / `_read_gate`.
  - `active_scene` maps the read-back code to a name (`1.2.1 devices/device.py:182-188`,
    `dev devices/device.py:310-317`). No bundled catalogue in 1.2.1 or dev has two scenes with the
    same code (checked for this document), so the name identifies the scene.
- **Proposed library change:** none required. Optional suggestion (not from the review): a
  `set_scene_by_code(code)` that uploads when the code is catalogued, so callers that only have the
  read-back code do the right thing.
- **Integration change and test:** add `scene_name: str | None` (from `device.active_scene`) to the
  snapshot. Inside the existing `scene_code` / SCENES guard, restore with `set_scene_by_name(name)`
  when the name is known, else `set_scene(code)` (DIY or uncatalogued code). Change the expected call
  in [tests/test_capture.py:494-497](../tests/test_capture.py#L494-L497) to `("set_scene_by_name", ("Afternoon",))` and add an
  uncatalogued-code case that still expects `set_scene`.
- **Verification:** confirmed by 1 verifier.
- **Decision:** open

### 1.5 `unknown-original-state-not-guarded`

- **Severity / source:** low. Review finding.
- **Symptom in Home Assistant:**
  - Zones: when the starting power is unknown, the whole-device power off is skipped
    ([custom_components/govee_ble_local/capture.py:515-518](../custom_components/govee_ble_local/capture.py#L515-L518)), but every zone is still switched off ([custom_components/govee_ble_local/capture.py:488-492](../custom_components/govee_ble_local/capture.py#L488-L492)),
    and the restore skips zones whose starting power is unknown ([custom_components/govee_ble_local/capture.py:309-311](../custom_components/govee_ble_local/capture.py#L309-L311)). An
    H60A6 is left dark.
  - Gradual: `toggled = not bool(device.state.gradual)` ([custom_components/govee_ble_local/capture.py:501](../custom_components/govee_ble_local/capture.py#L501)) runs when the flag was
    never read, and the restore skips gradual when the snapshot has None
    ([custom_components/govee_ble_local/capture.py:328](../custom_components/govee_ble_local/capture.py#L328)). The persistent fade flag is left on. The integration has no gradual
    entity, so nothing else turns it back.
- **Library behaviour:**
  - `set_zone_power` sets `is_on = any(zone_power)` once every zone is known
    (`1.2.1 devices/device.py:514-515`).
  - The gradual read leaves `state.gradual` None when the device does not answer
    (`1.2.1 devices/device.py:657-662`), because `query` returns the frames it collected on timeout
    instead of raising (`1.2.1 transport/connection.py:724-741`).
  - At 1.2.1 only H61A8 has `gradual=True` (`1.2.1 devices/profile.py:129`), and its profile is
    "not yet live-verified" (`:122-123`).
  - Still present in dev @ c823fc5 (`dev devices/device.py:1026-1031`,
    `dev transport/connection.py:885-890`). Dev also gives H6047 `gradual=True`
    (`dev devices/profile.py:362-363`, `:371`), so after the refactor the toggle runs on H6047 too.
- **Proposed library change:** none required. The per-field read outcome in 2.1 would let the
  integration tell "the device did not answer" from "not read yet".
- **Integration change and test:** guard both steps on the snapshot, not on live state (per-step
  `update()` calls can fill `state.gradual` mid-test while the snapshot stays None):
  - Gradual: skip with "initial gradual state unknown" when `snap["gradual"] is None`, else toggle
    `not snap["gradual"]`.
  - Zones: skip `zone_power_off:<zone>` only when the snapshot's zone power is None and whole-device
    power cannot put it back (`snap["is_on"] is None` or no POWER).
  - Tests: tighten `test_initial_power_unknown_skips_power_off` ([tests/test_capture.py:316-332](../tests/test_capture.py#L316-L332))
    to assert no zone is left off; add an H61A8 case (and H6047 on the newest leg) with
    `gradual=None` asserting `set_gradual` is never awaited.
- **Verification:** confirmed by 1 verifier. Needs hardware: does H61A8 (and H6047) answer the
  `aa a3` gradual query?
- **Decision:** open

### 1.6 `unknown-power-relay-left-on`

- **Severity / source:** low. Review finding.
- **Symptom in Home Assistant:** for a plug whose starting state is unknown, the self-test switches
  the relay on ([custom_components/govee_ble_local/capture.py:413-416](../custom_components/govee_ble_local/capture.py#L413-L416)), skips the power off ([custom_components/govee_ble_local/capture.py:515-518](../custom_components/govee_ble_local/capture.py#L515-L518)), and the
  restore cannot undo it. The plug is left energised, which is the less safe outcome for loads such
  as heaters or pumps. The service is admin-only because it "switches lights/plug relays"
  ([custom_components/govee_ble_local/services.py:94](../custom_components/govee_ble_local/services.py#L94)). The test [tests/test_capture.py:316-333](../tests/test_capture.py#L316-L333) pins power-on-only, but on an
  H60A6.
- **Library behaviour:**
  - The plug profile is POWER only, `relay=True`, `readback="plug"`, and "not yet live-verified"
    (`1.2.1 devices/profile.py:164-172`).
  - `_read_plug` leaves `is_on` None when there is no usable reply (`1.2.1 devices/device.py:702-710`).
  - Still present in dev @ c823fc5 (`dev devices/device.py:1058-1069`). Dev adds per-goodsType
    relay masks (`dev devices/profile.py:579-630`); the relay read-back is still marked not
    live-verified (`:597-598`). An H5083 has been on the library bench: its time sync was checked
    live on 2026-10-17 (library commit `8c89c31`).
- **Proposed library change:** none required.
- **Integration change and test:** pass `power_known` into `_exercise_fixture` and skip `power_on`
  for relay devices whose starting state is unknown. Alternative: one more `device.update()` when
  `is_on` is None, then return an `ok=False` report for a relay that is still unknown. Test: a plug
  SKU (H5083) with `is_on=None` asserts `set_power` is never awaited and both power steps report
  skipped.
- **Verification:** confirmed by 1 verifier. Needs hardware: H5083, does `aa 01` answer reliably, and
  does the bit mapping match?
- **Decision:** open

## 2. Self-test verification & abort

### 2.1 `readback-ok-checks-optimistic-echo`

- **Severity / source:** medium. Review finding (also review note `oos.design.capture.0`), plus the
  library API request from review note `oos.correctness.capture.3` (DeviceState does not record
  which fields the device confirmed).
- **Symptom in Home Assistant:** the report says `readback_ok: true` for steps whose read failed and
  for fields the device never reports, so the self-test cannot detect a dropped RGB command on H60A6,
  H6047, H6641 or H61A8. A failed `update()` is only logged at DEBUG, and `verify()` still runs
  ([custom_components/govee_ble_local/capture.py:353-364](../custom_components/govee_ble_local/capture.py#L353-L364)). The test `test_ble_errors_on_reads_are_tolerated`
  ([tests/test_capture.py:303-313](../tests/test_capture.py#L303-L313)) pins this.
- **Library behaviour:**
  - Every setter writes the commanded value into state right after the send
    (`1.2.1 devices/device.py:299-314`, `:385`, `:398`, `:413`, `:419-473`, `:513`, `:594`, `:627`;
    dev: every setter goes through `_commit`, `dev devices/device.py:507-527`).
  - `DeviceState.optimistic` starts True, turns False on the first applied read, and no setter sets
    it back (`1.2.1 devices/device.py:90`, `:709`, `:866`, `:893`; `dev devices/device.py:165`,
    `:485-493`). It cannot tell one step's read from another.
  - A read that times out returns without raising (`1.2.1 transport/connection.py:724-741`,
    `dev transport/connection.py:885-890`), and a read discarded as stale is dropped silently
    (`1.2.1 devices/device.py:271-281`). In both cases `update()` succeeds and the echo stays.
  - Still present in dev @ c823fc5. Dev already records each read's outcome internally
    (`_ReadResult` = "applied" / "stale" / "none", `dev devices/device.py:49-51`, `:1207-1233`) but
    does not expose it. Which fields a SKU reads back is now chosen per profile by
    `mode_color_parse` (`dev devices/profile.py:29-49`), for example H60A6 decodes RGB only from a
    0x0d report.
- **Proposed library API:**
  - `Device.last_update: Mapping[str, Literal["applied", "stale", "none"]]`, keyed by `DeviceState`
    field name (`is_on`, `brightness`, `rgb_color`, `color_temp_kelvin`, `scene_code`, `segments`,
    `zone_power`, `gradual`), reset at the start of each `update()`. "applied" = a device reply set
    this field during this `update()`; "stale" = the reply was discarded because a setter ran;
    "none" = no usable reply, including a query timeout.
  - `Device.readback_fields: frozenset[str]`: the fields this SKU's profile can ever read back.
- **Integration change and test:**
  - With the API: `readback_ok` is None unless the step's field is "applied" in `last_update`, and
    `verify=None` for fields outside `readback_fields`.
  - Before it (floor 1.2.1), the verifier's fix: record `read_ok = False` and `readback_error` when
    `update()` raises, and only verify after a read that did not raise; pass `verify=None` for the
    whole-device `rgb` step unless the profile decodes RGB. The verifier used
    `color_scheme == "h6006"` for that at 1.2.1; that rule goes stale on dev, where RGB decoding is
    per SKU (`"h6188"`-scheme H6188 decodes RGB, H60A6 decodes it from 0x0d). Drop the
    `state.optimistic` check from the original fix; it guards nothing per step.
  - Tests: `test_ble_errors_on_reads_are_tolerated` expects `readback_ok is None` (and `ok` True); an
    H60A6 `rgb` step gives None; a `last_update` of "none" gives None.
- **Verification:** confirmed by 1 verifier. The API request is from a review note (not verified).
- **Decision:** open

### 2.2 `self-test-no-abort-on-dead-link` (remainder)

- **Severity / source:** medium. Review finding (merged from three findings and review notes
  `oos.correctness.capture.1`, `oos.security.core.2`). The presence pre-check (do not start when
  `coordinator.address_present()` is False) is being added in the current release work. What is left
  needs the library's exception taxonomy and is described here.
  - Done *(working tree)*: `async_self_test_entry` refuses to start with the translated
    `self_test_device_not_found` error when no connectable scanner sees the device
    ([custom_components/govee_ble_local/services.py:45-52](../custom_components/govee_ble_local/services.py#L45-L52)), tested by `test_press_fails_fast_when_no_scanner_sees_the_device`
    ([tests/test_button.py:155-174](../tests/test_button.py#L155-L174)).
  - Reported back by the integration's lifecycle pass (B1) as not done, because it needs the library:
    stop exercising and short-circuit the restore once the link is down, and bail out when the
    initial `device.update()` fails with a link-level error.
- **Symptom in Home Assistant:** against a device that still advertises but cannot be connected (no
  free proxy slot, wedged BLE stack), or that drops out mid-run, every step and every restore action
  still runs. Each library operation is bounded only by the 45 s operation timeout; an H60A6 run is
  about 29 operations, roughly 12-22 minutes (verifier's estimate). For that time:
  - the module-wide `_SELF_TEST_LOCK` makes every other device's self-test fail as busy
    ([custom_components/govee_ble_local/capture.py:62](../custom_components/govee_ble_local/capture.py#L62), [custom_components/govee_ble_local/capture.py:547-550](../custom_components/govee_ble_local/capture.py#L547-L550));
  - `session_capture` keeps the shared library logger at DEBUG for every device
    ([custom_components/govee_ble_local/capture.py:160-187](../custom_components/govee_ble_local/capture.py#L160-L187));
  - the button press or service call does not return.

  Code: a failed initial read is only logged ([custom_components/govee_ble_local/capture.py:559-564](../custom_components/govee_ble_local/capture.py#L559-L564)); `_Steps.run` records a BLE
  error and returns, so the next step runs ([custom_components/govee_ble_local/capture.py:353-373](../custom_components/govee_ble_local/capture.py#L353-L373)); `_Restorer.attempt` catches
  everything and moves on ([custom_components/govee_ble_local/capture.py:262-269](../custom_components/govee_ble_local/capture.py#L262-L269)). The same code *(working tree)*: the initial read
  at [custom_components/govee_ble_local/capture.py:587-592](../custom_components/govee_ble_local/capture.py#L587-L592), `_Steps.run` at [custom_components/govee_ble_local/capture.py:369-401](../custom_components/govee_ble_local/capture.py#L369-L401), `_Restorer.attempt` at
  [custom_components/govee_ble_local/capture.py:290-297](../custom_components/govee_ble_local/capture.py#L290-L297), the lock at [custom_components/govee_ble_local/capture.py:61](../custom_components/govee_ble_local/capture.py#L61) and `session_capture` at
  [custom_components/govee_ble_local/capture.py:185-215](../custom_components/govee_ble_local/capture.py#L185-L215).
- **Library behaviour:** link-level failures and per-command failures share exception classes and
  differ only in the message text.
  - 1.2.1:
    - connect failed: `GoveeBleConnectionError("connect to … failed: …")`
      (`1.2.1 transport/connection.py:375`);
    - connect or handshake timed out: `GoveeBleTimeout` (`:374`);
    - link lost: `GoveeBleConnectionError("…: link lost mid-operation")` (`:563-564`);
    - closed by `stop()`: `GoveeBleConnectionError("…: connection closed")` (`:329-331`);
    - 45 s deadline: `GoveeBleTimeout("…: operation exceeded 45s")` (`:565-569`);
    - no ack on a live link: `GoveeBleTimeout("…: no ack for write 0x.. after 2 attempts")`
      (`:683-685`).
  - Partly changed in dev @ c823fc5: dev adds `GoveeBleHandshakeTimeout` and
    `GoveeBleHandshakeConnectionError`, which keep the old bases (`dev exceptions.py:27-38`, raised at
    `dev transport/connection.py:505-518`), and `GoveeBleCommandRejected` for a non-zero ack status
    (`dev exceptions.py:45-46`, `dev transport/connection.py:816-819`). Connect failure (`:434-447`),
    connection closed (`:382`), link lost (`:671`), the deadline (`:674-677`), a BLE I/O timeout
    (`:679`) and no-ack (`:825-827`) still share `GoveeBleConnectionError` / `GoveeBleTimeout`.
  - The message text is also changing in dev, so a text match written against 1.2.1 breaks on the
    newest leg: connect failure is `"…: connect failed: …"` (`dev transport/connection.py:439`, `:447`)
    instead of 1.2.1's `"connect to … failed: …"`; the connect timeout is `"…: connect timed out"`
    (`:436`) instead of `"…: connect/handshake timed out"`, and the handshake timeout is now a separate
    `GoveeBleHandshakeTimeout("…: handshake timed out")` (`:518`). Only `"operation exceeded"`
    (`:674-677`) and the no-ack text (`:825-827`) are unchanged. This is why the integration pass
    shipped no message-based heuristic.
  - The verifier's warning: treating every `GoveeBleTimeout` as a dead link would abort the whole test
    on a single ignored command. A no-ack on a healthy link costs a few seconds and is exactly what a
    step should report.
- **Proposed library API:** subclasses that keep today's bases, the same pattern dev uses for the
  handshake errors, exported in `govee_ble_local.__all__`:

  ```python
  class GoveeBleLinkDown(GoveeBleError):
      """Marker: no usable link. Further I/O fails the same way until a reconnect succeeds."""

  class GoveeBleConnectFailed(GoveeBleConnectionError, GoveeBleLinkDown): ...    # connect failed
  class GoveeBleConnectTimeout(GoveeBleTimeout, GoveeBleLinkDown): ...           # connect timed out
  class GoveeBleLinkLost(GoveeBleConnectionError, GoveeBleLinkDown): ...         # lost mid-operation
  class GoveeBleConnectionClosed(GoveeBleConnectionError, GoveeBleLinkDown): ... # stop() was called
  class GoveeBleOperationTimeout(GoveeBleTimeout, GoveeBleLinkDown): ...         # 45 s deadline hit
  class GoveeBleNoAck(GoveeBleTimeout): ...  # live link, write not acked: per command, not link-down
  ```

  The dev handshake classes could also inherit `GoveeBleLinkDown`. `GoveeBleCommandRejected` stays
  per command.
- **Integration change and test:**
  - `_Steps.run`: on `GoveeBleLinkDown`, record the error, set `aborted`, and record every later step
    as skipped ("aborted: link down: …"). Never abort on `GoveeBleNoAck` or
    `GoveeBleCommandRejected`.
  - Initial `device.update()` raising `GoveeBleLinkDown`: raise before exercising; nothing has
    changed, so there is nothing to restore. Today ([custom_components/govee_ble_local/capture.py:587-592](../custom_components/govee_ble_local/capture.py#L587-L592) *(working tree)*) it is
    only logged at DEBUG, the snapshot is taken from whatever state is cached, and every step runs.
    Bail out only on link-down; a per-command error here (`GoveeBleNoAck`, `GoveeBleCommandRejected`)
    still lets the run go ahead.
  - `_Restorer.attempt`: stop at the first `GoveeBleLinkDown` and list the remaining actions as not
    restored. `GoveeBleConnectionClosed` also ends a run cut short by an entry unload (the separate
    finding `gap.1.unload-during-self-test-skips-restore`).
  - Optional: separate deadlines for the exercise phase and the restore in the `finally`.
  - Until the library ships this, the verifier's interim heuristic (abort after two consecutive steps
    that never got an ack; stop the restorer on "connect to…" / "operation exceeded" messages) is the
    fallback. It depends on message text, and the "connect to…" match already fails against dev (see
    above), so the integration pass did not ship it. If it is ever shipped before the API, match on
    both spellings and test on both CI legs.
  - Ideal library shape, as the integration pass put it: one distinct link-down exception (the
    `GoveeBleLinkDown` marker above) or an equivalent attribute, so the integration never parses
    messages.
  - Tests: a step raising `GoveeBleConnectFailed` skips the later steps, the restore stops after the
    first link-down, and `ok` is False; a step raising `GoveeBleNoAck` lets later steps run; an
    initial read raising `GoveeBleLinkDown` runs no step and raises.
- **Related:** review notes `oos.security.core.0` and `oos.correctness.platforms.0` say
  `GoveeBleInvalidValue` (and, on dev, `GoveeBleCommandRejected`) fall into `BLE_ERRORS`
  ([custom_components/govee_ble_local/helpers.py:9](../custom_components/govee_ble_local/helpers.py#L9), [custom_components/govee_ble_local/entity.py:96-106](../custom_components/govee_ble_local/entity.py#L96-L106)). That finding (`invalid-value-classified-as-ble-failure`)
  was refuted: no user input reaches the library as `GoveeBleInvalidValue`, and a device refusal is
  correctly a `HomeAssistantError`. No change is needed for it.
- **Verification:** confirmed by 1 verifier, with partial refutations: the button is gated by entity
  availability (the `capture_session` service is not), and a "connect storm" happens only in the
  wedged-device case, because an absent device or a missing slot fails before any radio use.
- **Decision:** open

### 2.3 `readback-without-verify`

- **Severity / source:** nit. Review finding.
- **Symptom in Home Assistant:** no user-visible symptom. `_Steps.run` always runs a full
  `device.update()` after the action ([custom_components/govee_ble_local/capture.py:355-364](../custom_components/govee_ble_local/capture.py#L355-L364)), even for steps with no `verify`
  (`segment_0_color_temp` at [custom_components/govee_ble_local/capture.py:459-463](../custom_components/govee_ble_local/capture.py#L459-L463), `zone_color_temp:<zone>` at
  [custom_components/govee_ble_local/capture.py:483-487](../custom_components/govee_ble_local/capture.py#L483-L487)). That is 1 + N zones extra full reads per run.
- **Library behaviour:** at 1.2.1 `update()` is the state read, then the segment read-back, then the
  gradual query (`1.2.1 devices/device.py:631-680`). Changed in dev: the H60A6 poll is the 0xAC burst
  alone, with the mode in its 0x05 TLV and no `aa 05` read after it
  (`dev devices/profile.py:321-327`, `dev devices/device.py:1142-1144`), so the extra reads are
  cheaper there. Other SKUs still make several reads.
- **Proposed library change:** none.
- **Integration change and test:** decide whether these reads' frames are wanted in the capture (for
  govee-ble-analyze). If not, skip `update()` when `verify` is None; if so, say so in the docstring.
  Measure again on the refactored library. If changed, assert `update` is not awaited after an
  unverified step.
- **Verification:** not verified (nit).
- **Decision:** open

## 3. Self-test report & service shape

These three items were classed as library-dependent because they reshape code that sections 1 and 2
rewrite. No library change was found for them. Do them in the same pass as the self-test rework so the
types are written once.

### 3.1 `self-test-report-untyped`

- **Severity / source:** low. Review finding (five merged sources).
- **Symptom in Home Assistant:** no user-visible symptom. The report is a fixed schema but travels as
  `dict[str, Any]`: produced at [custom_components/govee_ble_local/capture.py:533](../custom_components/govee_ble_local/capture.py#L533), [custom_components/govee_ble_local/capture.py:553](../custom_components/govee_ble_local/capture.py#L553) and
  [custom_components/govee_ble_local/capture.py:590-601](../custom_components/govee_ble_local/capture.py#L590-L601); steps typed at [custom_components/govee_ble_local/capture.py:339](../custom_components/govee_ble_local/capture.py#L339) and [custom_components/govee_ble_local/capture.py:353](../custom_components/govee_ble_local/capture.py#L353); returned
  by [custom_components/govee_ble_local/services.py:32-34](../custom_components/govee_ble_local/services.py#L32-L34); stored at [custom_components/govee_ble_local/coordinator.py:109](../custom_components/govee_ble_local/coordinator.py#L109); read by key at
  [custom_components/govee_ble_local/button.py:51-61](../custom_components/govee_ble_local/button.py#L51-L61). mypy cannot check any key. Step entries come in two shapes
  ([custom_components/govee_ble_local/capture.py:353](../custom_components/govee_ble_local/capture.py#L353) vs [custom_components/govee_ble_local/capture.py:377-386](../custom_components/govee_ble_local/capture.py#L377-L386)).
- **Library behaviour:** none. The step list and report fields change with the items in sections 1
  and 2.
- **Proposed library change:** none.
- **Integration change and test:** `StepResult` and `SelfTestReport` TypedDicts (plus a TypedDict for
  the `{ok, address, error}` rows in `services.py`), threaded through `async_run_self_test`,
  `async_self_test_entry` and `coordinator.last_self_test`. No new test; mypy covers it.
- **Verification:** confirmed by 1 verifier. It noted that the existing tests already catch a key typo
  under the coverage gate, so this is maintainability, not a live bug.
- **Decision:** open

### 3.2 `stringly-typed-verify-helpers`

- **Severity / source:** nit (raised as low). Review finding.
- **Symptom in Home Assistant:** no user-visible symptom. `_state_is` / `_segment0_is` fetch
  attributes by string with `getattr` ([custom_components/govee_ble_local/capture.py:389-395](../custom_components/govee_ble_local/capture.py#L389-L395)) and `_match` takes `Any`
  ([custom_components/govee_ble_local/capture.py:190-197](../custom_components/govee_ble_local/capture.py#L190-L197)), so mypy cannot check the eight attribute names at the call sites
  ([custom_components/govee_ble_local/capture.py:415](../custom_components/govee_ble_local/capture.py#L415), [custom_components/govee_ble_local/capture.py:421](../custom_components/govee_ble_local/capture.py#L421), [custom_components/govee_ble_local/capture.py:427](../custom_components/govee_ble_local/capture.py#L427), [custom_components/govee_ble_local/capture.py:434](../custom_components/govee_ble_local/capture.py#L434),
  [custom_components/govee_ble_local/capture.py:452](../custom_components/govee_ble_local/capture.py#L452), [custom_components/govee_ble_local/capture.py:457](../custom_components/govee_ble_local/capture.py#L457), [custom_components/govee_ble_local/capture.py:505](../custom_components/govee_ble_local/capture.py#L505), [custom_components/govee_ble_local/capture.py:513](../custom_components/govee_ble_local/capture.py#L513)).
- **Library behaviour:** all eight fields exist unchanged in dev @ c823fc5 (`dev models.py:46-47`,
  `:72-78`). The refactor is where a rename would happen, which is why this is listed here; the
  real-Device tests would still catch one.
- **Proposed library change:** none.
- **Integration change and test:** `_match(actual: object, expected: object)`; replace each
  `partial(_state_is, device, "<attr>", X)` with `lambda: _match(device.state.<attr>, X)`; reuse
  `_segment_at` for segment 0; delete `_state_is` and `_segment0_is`. No test change.
- **Verification:** confirmed by 1 verifier, lowered to nit.
- **Decision:** open

### 3.3 `capture-response-mixed-error-format`

- **Severity / source:** nit. Review finding.
- **Symptom in Home Assistant:** the `capture_session` response's `error` is the English token
  "not loaded" in one branch and a translated sentence in the other ([custom_components/govee_ble_local/services.py:80-86](../custom_components/govee_ble_local/services.py#L80-L86)), so an
  automation cannot branch on it.
- **Library behaviour:** none.
- **Proposed library change:** none.
- **Integration change and test:** add a stable `reason` code next to the message (`not_loaded`, or
  `err.translation_key`). Update [tests/test_services.py:239](../tests/test_services.py#L239).
- **Verification:** not verified (nit).
- **Decision:** open

## 4. Sub-light (segment/zone) state model

### 4.1 `segment-light-ignores-fixture-power`

- **Severity / source:** medium. Review finding (also review notes `oos.design.core.4`,
  `oos.style.platforms.2`).
- **Symptom in Home Assistant:** after the fixture is turned off (from Home Assistant, the Govee app
  or the wall), or at startup when it is already off, every enabled segment light with a non-black
  colour keeps showing on. `light.toggle` then sends a black-out instead of turning the segment on,
  and a plain `turn_on` sends nothing and marks the segment on while the fixture stays dark. Segment
  lights are disabled by default ([custom_components/govee_ble_local/light.py:297](../custom_components/govee_ble_local/light.py#L297)). `GoveeBleLocalSegmentLight` has no `is_on`
  override ([custom_components/govee_ble_local/light.py:285-372](../custom_components/govee_ble_local/light.py#L285-L372)); its state comes from read-back ([custom_components/govee_ble_local/light.py:318-323](../custom_components/govee_ble_local/light.py#L318-L323)) and
  its own commands. Its siblings have the fixture-off gate ([custom_components/govee_ble_local/light.py:404-413](../custom_components/govee_ble_local/light.py#L404-L413),
  [custom_components/govee_ble_local/switch.py:58-63](../custom_components/govee_ble_local/switch.py#L58-L63)).
- **Library behaviour:** segments have no power line. Whole-device power changes only `is_on` and
  `zone_power` (`1.2.1 devices/device.py:299-302`); an advertisement changes only `is_on`
  (`:237-253`); the status read stores each segment's colour whatever the power
  (`:850-853`); segment setters never power on (`:419-473`). Still present in dev @ c823fc5
  (`dev devices/_fold.py:36-44`, `:145-155`, `:182-186`).
- **Proposed library change:** none required. Suggestion (not from the review): say in the
  `DeviceState.segments` docstring that it holds each segment's stored colour, not whether it is lit.
  A batched "power, then segment colour" write (5.3) would light a segment of a dark fixture in one
  operation.
- **Integration change and test:** add `is_on` returning False when `coordinator.data.is_on is False`,
  and in `async_turn_on` power the fixture first when it is off (with POWER); without that, the gated
  entity can never be turned on. Keep `_is_dark()` on `_attr_is_on`. Optional: one shared helper for
  the gate in the segment light, zone light and zone switch. Tests: a segment seeded with
  `is_on=False, segments=[Segment(rgb=RED)]` shows off; `turn_on` with the fixture off sends power.
- **Verification:** confirmed by 1 verifier, who made the power-first step part of the fix rather than
  optional.
- **Decision:** open

### 4.2 `sublight-cct-mode-never-reconciles`

- **Severity / source:** medium. Review finding (two merged sources), plus the library API request
  from review note `oos.design.platforms.4` (a per-segment kelvin field).
- **Symptom in Home Assistant:** after a per-zone or per-segment colour-temperature command, the
  sub-light latches `color_temp` and ignores every later read-back colour. Example: on an H60A6 or
  H6047 (zone lights are enabled by default), set the background zone to 3000 K, then set the whole
  fixture to blue, run a scene, or change it in the Govee app: the zone light keeps reporting
  3000 K. A latched segment light also ignores read-back on/off. The reverse also holds if a CT
  segment reads back black (the test assumption at [tests/test_light.py:909](../tests/test_light.py#L909)): a whole-fixture CT
  makes RGB-mode sub-lights show black or off. Code: [custom_components/govee_ble_local/light.py:318-319](../custom_components/govee_ble_local/light.py#L318-L319),
  [custom_components/govee_ble_local/light.py:401-402](../custom_components/govee_ble_local/light.py#L401-L402); `_attr_color_mode` is written only at [custom_components/govee_ble_local/light.py:108](../custom_components/govee_ble_local/light.py#L108),
  [custom_components/govee_ble_local/light.py:340](../custom_components/govee_ble_local/light.py#L340), [custom_components/govee_ble_local/light.py:350](../custom_components/govee_ble_local/light.py#L350), [custom_components/govee_ble_local/light.py:369](../custom_components/govee_ble_local/light.py#L369), [custom_components/govee_ble_local/light.py:425](../custom_components/govee_ble_local/light.py#L425),
  [custom_components/govee_ble_local/light.py:431](../custom_components/govee_ble_local/light.py#L431).
- **Library behaviour:**
  - At 1.2.1 `set_segment_color_temp` and `set_zone_color_temp` keep no per-segment kelvin ("no
    optimistic field", `1.2.1 devices/device.py:475-486`, `:570-584`); `Segment` has only `rgb` and
    `brightness` (`1.2.1 models.py:41-47`).
  - Partly changed in dev @ c823fc5: still no per-segment kelvin (`dev models.py:41-47`,
    `dev devices/device.py:769-787`). Dev adds `DeviceState.zone_color_temp_kelvin` and
    `Device.zone_color_temp_kelvin(name)` for bar-switch devices (H6047) only, filled from zone writes
    and from the per-bar kelvin in `aa 05 15` (`dev models.py:81-85`, `dev devices/device.py:796-802`,
    `:912-914`, `dev devices/_fold.py:116-141`, `:236-240`), and a whole-device
    `last_color_temp_kelvin` (`dev models.py:97-103`).
  - Neither version documents what a CT segment reads back as.
- **Proposed library API:** `Segment.kelvin: int | None` (None = not in colour-temperature mode, or
  unknown). Set by `set_segment_color_temp`, `set_zone_color_temp` and whole-device `set_color_temp`
  (all-segments mask); cleared by segment, zone and whole-device RGB writes and by scenes; filled from
  read-back where the device reports it. Document per read-back mechanism what a CT segment reports.
- **Integration change and test:**
  - With `Segment.kelvin`: derive each sub-light's colour mode and kelvin from coordinator data and
    remove the latch.
  - Before it: when a zone or segment CT command lands, record
    `(data.rgb_color, data.color_temp_kelvin, data.scene_code)`, and drop the latch as soon as that
    tuple changes. Only if hardware confirms that a CT segment reads back black, also release it on a
    non-black colour that differs from the first poll after the command. On dev, use
    `device.zone_color_temp_kelvin(name)` for H6047 zones behind a `getattr` guard so the floor can
    stay.
  - Tests: zone CT then a whole-fixture RGB gives the zone back RGB; a scene releases the latch; an
    H6047 zone takes its kelvin from read-back on the newest leg.
- **Verification:** confirmed by 1 verifier. The "CT reads back black" model is unverified. Needs
  hardware: H60A6, H6047, H6641, see section 10.
- **Decision:** open

### 4.3 `segment-cct-lost-after-off`

- **Severity / source:** low. Review finding (from review note `oos.tests.tests-flow-light.2`).
- **Symptom in Home Assistant:** a segment set to a colour temperature, turned off, then turned on
  comes back as `_last_color or WHITE` in RGB, not its kelvin ([custom_components/govee_ble_local/light.py:342-345](../custom_components/govee_ble_local/light.py#L342-L345),
  [custom_components/govee_ble_local/light.py:364-370](../custom_components/govee_ble_local/light.py#L364-L370)).
- **Library behaviour:** no per-segment kelvin, as in 4.2. `set_segment_color_temp` exists at the
  floor (`1.2.1 devices/device.py:475`) and in dev (`dev devices/device.py:769`). Dev's
  `last_color_temp_kelvin` is whole-device only. Still present in dev @ c823fc5.
- **Proposed library change:** covered by `Segment.kelvin` in 4.2.
- **Integration change and test:** keep `_restore_kelvin`, recorded in `async_turn_off` only while the
  segment is lit (so a second off does not clear it); on a plain `turn_on` of a dark segment, send
  `set_segment_color_temp` with it and set the mode to `COLOR_TEMP`. Clear it when an RGB colour or an
  explicit kelvin is commanded, and when read-back adopts a non-black colour. With `Segment.kelvin`,
  derive it from state instead. Test: colour temperature, off, on calls `set_segment_color_temp`
  again and reports `color_mode` `color_temp`.
- **Verification:** confirmed by 1 verifier.
- **Decision:** open

### 4.4 `whole-device-colour-not-folded-into-segments`

- **Severity / source:** unrated. Review notes `oos.correctness.platforms.5` and
  `oos.design.platforms.3`.
- **Symptom in Home Assistant:** after a whole-fixture colour change from Home Assistant, the zone and
  segment lights keep their old colours until the next segment read-back (the poll runs every 120 s,
  up to 1800 s under backoff). The integration republishes every entity at once
  ([custom_components/govee_ble_local/entity.py:62-70](../custom_components/govee_ble_local/entity.py#L62-L70)), but the sub-lights read `state.segments` ([custom_components/govee_ble_local/light.py:309-323](../custom_components/govee_ble_local/light.py#L309-L323),
  [custom_components/govee_ble_local/light.py:394-402](../custom_components/govee_ble_local/light.py#L394-L402)). Where the poll returns no segments at all (on dev, an H60A6 that has
  advertised a pact other than (1, ≥2), `dev devices/profile.py:329-333`), they never update.
- **Library behaviour:** `turn_on`, `set_rgb`, `set_color_temp` and `set_scene` update `rgb_color`,
  `color_temp_kelvin` and `scene_code`, not `segments` (`1.2.1 devices/device.py:327-371`,
  `:390-417`, `:587-599`). Still present in dev @ c823fc5 (`dev devices/_fold.py:62-91`).
- **Proposed library change:** fold whole-device writes into `state.segments`: an RGB write gives every
  addressable segment that colour (it is sent with the all-segments mask, `1.2.1 wire/build.py:121-129`);
  a CT write sets `Segment.kelvin` (4.2) and clears the segment RGB; a scene leaves the segment colours
  unknown (`rgb=None`).
- **Integration change and test:** none beyond 4.2 once the library folds. Test: a whole-fixture RGB
  updates the zone and segment lights without a poll.
- **Verification:** review notes, not verified.
- **Decision:** open

### 4.5 `zone-light-off-after-turn-on-when-fixture-off`

- **Severity / source:** low. Review finding (from review note `oos.tests.tests-flow-light.1`).
- **Symptom in Home Assistant:** on an H60A6 after a restart, while the fixture is advertised off and
  zone power has not been read yet (`is_on=False`, `zone_power={}`), turning a zone on succeeds but the
  zone light, and the fixture light, show off. It corrects itself at the first poll or the next "on"
  advertisement after the 15 s suppression, so it lasts about 15-30 s. The zone light returns False
  whenever the fixture is known off ([custom_components/govee_ble_local/light.py:411-413](../custom_components/govee_ble_local/light.py#L411-L413)).
- **Library behaviour:** `set_zone_power` recomputes `is_on` only once every zone's power is known
  (`1.2.1 devices/device.py:514-515`), and an "off" advertisement sets only `is_on` (`:251`). Same in
  1.3.0. Changed in dev for the advertisement path: an "off" advertisement marks every
  zone off (`dev devices/_fold.py:145-155`, called at `dev devices/device.py:466`), so the next zone
  write sees all zones and sets `is_on=True` (`dev devices/_fold.py:47-52`). A zone-on while zone
  power is partly unknown for another reason still leaves `is_on` as it was.
- **Proposed library change:** in `set_zone_power(name, True)`, also set `is_on = True` (a lit zone
  means the fixture is on), and release dev's `apply_advert_power`.
- **Integration change and test:** no `light.py` change once that is released. Do not use
  `is_on is False and not zone_is_on`: it breaks the deliberate "fixture off beats a stale zone power"
  case ([tests/test_light.py:1063](../tests/test_light.py#L1063)). Optional stopgap for 1.2.1 / 1.3.0: after a successful zone-on
  while `coordinator.data.is_on is False`, call `coordinator.async_request_refresh()`. Test: 4.7.
- **Verification:** confirmed by 1 verifier.
- **Decision:** open

### 4.6 `zone-power-stale-after-advert-off`

- **Severity / source:** unrated. Review note `oos.correctness.platforms.4`.
- **Symptom in Home Assistant:** after the fixture is advertised off, turning one zone off can flip
  the fixture back to "on", because the other zone's stale True is still in `zone_power`. The main
  light and the zone entities then show on while the fixture is off ([custom_components/govee_ble_local/light.py:165-168](../custom_components/govee_ble_local/light.py#L165-L168),
  [custom_components/govee_ble_local/light.py:411-413](../custom_components/govee_ble_local/light.py#L411-L413), [custom_components/govee_ble_local/switch.py:58-63](../custom_components/govee_ble_local/switch.py#L58-L63)).
- **Library behaviour:** at 1.2.1 `ingest_advertisement` sets only `is_on`
  (`1.2.1 devices/device.py:237-253`) and `set_zone_power` sets `is_on = any(zone_power)`
  (`:514-515`). Changed in dev: an "off" advertisement sets every zone off
  (`dev devices/_fold.py:145-155`).
- **Proposed library change:** release the dev behaviour.
- **Integration change and test:** none. Test on the newest leg: an "off" advertisement with
  `zone_power={0: True, 1: True}`, then one zone off, leaves the fixture off.
- **Verification:** review note, not verified.
- **Decision:** open

### 4.7 `zone-on-from-off-untested`

- **Severity / source:** low. Review finding.
- **Symptom in Home Assistant:** none directly; it hides 4.5. Every zone `turn_on` test seeds the
  fixture on ([tests/test_light.py:1086](../tests/test_light.py#L1086), [tests/test_light.py:1110](../tests/test_light.py#L1110),
  [tests/test_light.py:1135](../tests/test_light.py#L1135), and the H6047 case); the fixture-off case is display-only
  ([tests/test_light.py:1060-1078](../tests/test_light.py#L1060-L1078)). The zone switch has the same gap.
- **Library behaviour:** as 4.5.
- **Integration change and test:** add a test parametrised over the starting `zone_power`: `{}` and
  `{1: False}` (on only with the library fix or the stopgap) and `{0: False, 1: False}` (already on via
  the recompute). Assert `STATE_ON` and the frame `build.zone_power(0, True)`. Add the same
  fixture-off case for the zone switch in `tests/test_switch.py`. The first two cases fail on the 1.2.1
  floor, so land them with the integration stopgap, or with the floor raise, or make the expectation
  depend on the library version.
- **Verification:** confirmed by 1 verifier.
- **Decision:** open

### 4.8 `segment-index-space`

- **Severity / source:** unrated. Review note `oos.correctness.platforms.2`; the related finding
  `segment-readback-index-space-mismatch` was refuted (nit).
- **Symptom in Home Assistant:** none today. Segment and zone lights match read-back `Segment.index`
  directly to write indices ([custom_components/govee_ble_local/light.py:311](../custom_components/govee_ble_local/light.py#L311), [custom_components/govee_ble_local/light.py:396](../custom_components/govee_ble_local/light.py#L396); also
  [custom_components/govee_ble_local/capture.py:201](../custom_components/govee_ble_local/capture.py#L201), [custom_components/govee_ble_local/capture.py:210](../custom_components/govee_ble_local/capture.py#L210)). At 1.2.1 an H6047 reads 12 pieces back and writes
  10 segments; the phantom indices 10 and 11 are never looked up, because segment lights exist only
  for 0-9 ([custom_components/govee_ble_local/light.py:87-89](../custom_components/govee_ble_local/light.py#L87-L89)).
- **Library behaviour:**
  - `1.2.1 devices/profile.py:117` sets `color_readback_segments=12` for H6047; read-back indices are
    positional (`1.2.1 wire/parse.py:220-232`).
  - Changed in 1.3.0 (library CHANGELOG #13, `CHANGELOG.md:1239-1244`) and dev: read-back is the
    same 10 segments as the write (`dev devices/profile.py:344-349`), and dev's `_mask` rejects an
    index at or above `segment_count` (`dev devices/device.py:108-122`).
  - The records disagree on hardware: the integration's [custom_components/govee_ble_local/GAPS.md:29-31](../custom_components/govee_ble_local/GAPS.md#L29-L31) records all 12 pieces
    read from a real H6047 on 2026-10-12, while the dev profile says H6047 is "not yet live-verified
    (no hardware)" (`dev devices/profile.py:340`).
- **Proposed library change:** state in the `DeviceState.segments` docstring that indices are write
  (mask) indices and that read-back never yields an index at or above `segment_count`.
- **Integration change and test:** keep the `index < profile.segments` filter in 1.1 while the floor
  is 1.2.1, and drop it once the floor is 1.3.0 or later. Test: on the floor, a 12-piece H6047
  read-back restores only segments 0-9. Correct [custom_components/govee_ble_local/GAPS.md:29-31](../custom_components/govee_ble_local/GAPS.md#L29-L31) after the hardware check.
- **Verification:** the integration finding was refuted by 1 verifier. Needs hardware: H6047 piece
  count.
- **Decision:** open

## 5. Zone command batching & ordering

### 5.1 `zone-light-redundant-power-write`

- **Severity / source:** low. Review finding.
- **Symptom in Home Assistant:** every zone-light command ends with an extra ack-verified
  `set_zone_power(zone, True)` ([custom_components/govee_ble_local/light.py:439](../custom_components/govee_ble_local/light.py#L439)), even when the zone is already known on. A
  brightness change on a lit zone, the most common slider or automation action, costs two device
  round trips instead of one. The test [tests/test_light.py:1081-1097](../tests/test_light.py#L1081-L1097) expects
  `[seg_rgb, seg_bri, zone_power]`.
- **Library behaviour:** `set_zone_power` always sends (`1.2.1 devices/device.py:501-517`; on a
  bar-switch device a read-modify-write, `:519-539`). Every setter is its own send with its own lock
  and ack; `send_many` batching is only used by `turn_on` and `set_power`
  (`1.2.1 transport/connection.py:614-626`). Still present in dev @ c823fc5
  (`dev devices/device.py:507-527`, `:814-850`).
- **Proposed library change:** the batched write in 5.3.
- **Integration change and test:** send zone power for a plain `turn_on`, or when the zone is not
  known on, or while the state is optimistic; otherwise skip it (as the whole-fixture light already
  does at [custom_components/govee_ble_local/light.py:252-260](../custom_components/govee_ble_local/light.py#L252-L260)). With 5.3, one batched call. Tests: a brightness-only change on a lit
  zone sends one frame; re-seed `test_zone_partial_failure_still_publishes`
  ([tests/test_light.py:1216-1221](../tests/test_light.py#L1216-L1221)) with the zone off.
- **Verification:** confirmed by 1 verifier (the 3 s ack timeout is a ceiling, not the usual cost).
  Coordinate with 5.2, which may move this write.
- **Decision:** open

### 5.2 `zone-power-sent-after-colour`

- **Severity / source:** low. Review finding (from review note `oos.performance.platforms.1`).
- **Symptom in Home Assistant:** if the firmware drops zone colour or brightness while the zone is
  off, turning an off zone on with a colour shows its old colour. The zone light sends kelvin, RGB and
  brightness first and zone power last ([custom_components/govee_ble_local/light.py:415-441](../custom_components/govee_ble_local/light.py#L415-L441)).
- **Library behaviour:** whole-device `turn_on` sends power first (`1.2.1 devices/device.py:335-338`,
  `dev devices/device.py:619-622`); the zone setters are separate operations with no ordering
  guidance (`1.2.1 devices/device.py:553-584`, `dev devices/device.py:862-916`). No documented
  off-state semantics (see 1.2). Still present in dev @ c823fc5.
- **Proposed library change:** document the per-zone off-state semantics (as in 1.2), and order the
  frames of the batched write in 5.3 by them.
- **Integration change and test:** only after the hardware check. If the device drops frames sent to
  an off zone, send `set_zone_power(zone, True)` first when the zone is not known on (combine with
  5.1), update [tests/test_light.py:1081-1099](../tests/test_light.py#L1081-L1099), and fix the self-test restore order (1.2) in the
  same change. If the device keeps them, keep power last (the zone then comes on in the new colour
  without flashing the old one) and add a comment that the order is deliberate.
- **Verification:** 1 verifier, uncertain. This order has shipped since `d82fd30` without a report.
  Needs hardware: H60A6 and H6047, see section 10.
- **Decision:** open

### 5.3 `batched-sub-light-writes`

- **Severity / source:** unrated. Review note `oos.performance.platforms.2`.
- **Symptom in Home Assistant:** one zone-light service call makes up to four separate library
  operations (`set_zone_color_temp`, `set_zone_rgb`, `set_segment_brightness`, `set_zone_power`),
  each taking the connection lock and waiting for its own ack ([custom_components/govee_ble_local/light.py:415-439](../custom_components/govee_ble_local/light.py#L415-L439)). Each also
  gets its own `COMMAND_TIMEOUT_SECONDS` deadline ([custom_components/govee_ble_local/entity.py:80-82](../custom_components/govee_ble_local/entity.py#L80-L82); the separate finding
  `command-timeout-scope-narrower-than-documented` covers that).
- **Library behaviour:** `send_many` sends several frames under one lock and connection, each still
  ack-verified (`1.2.1 transport/connection.py:614-626`, `dev transport/connection.py:746`), but only
  whole-device `turn_on` / `set_power` use it. No zone or segment equivalent in 1.2.1 or dev.
- **Proposed library API:**
  - `Device.set_zone(name, *, power: bool | None = None, rgb=None, kelvin=None, brightness=None)`;
  - `Device.set_segments(indices, *, rgb=None, kelvin=None, brightness=None, power_fixture=False)`.

  Each sends one `send_many` batch with the frames in the order the measured semantics need (1.2,
  5.2), folds state once after the batch, and raises `GoveeBleInvalidValue` for `rgb` and `kelvin`
  together, as `turn_on` does. On a bar-switch device the bar read-modify-write stays under
  `_bar_lock`.
- **Integration change and test:** zone and segment lights make one batched call per service call,
  which also covers 5.1 and the power-first need in 4.1. Test: one call produces the expected frame
  list in one batch.
- **Verification:** review note, not verified.
- **Decision:** open

## 6. Connection & slot usage

### 6.1 `poll-semaphore-released-while-link-held`

- **Severity / source:** low (raised as medium). Review finding.
- **Symptom in Home Assistant:** `MAX_CONCURRENT_POLLS` ([custom_components/govee_ble_local/const.py:41-46](../custom_components/govee_ble_local/const.py#L41-L46)) limits concurrent
  polls, not open links. The semaphore is released when `update()` returns
  ([custom_components/govee_ble_local/coordinator.py:250-252](../custom_components/govee_ble_local/coordinator.py#L250-L252)), but the library keeps the link open 8 s more with nothing queued.
  During the startup stagger that means about 2 active plus 2.7-4 idle links. ESPHome proxies have 3
  slots by default, so polls and commands can be delayed or fail with no free slot. The device is
  built with the default idle delay ([custom_components/govee_ble_local/__init__.py:119](../custom_components/govee_ble_local/__init__.py#L119)).
- **Library behaviour:** the idle timer uses `delay = max(idle_disconnect, active_until - now)`
  (`1.2.1 transport/connection.py:753-761`): 8 s after any operation, 30 s after a write
  (`1.2.1 const.py:59-64`). `create_device(..., idle_disconnect=...)` exists at 1.2.1
  (`1.2.1 registry.py:30-50`); 0 or less turns idle disconnect off (`1.2.1 transport/connection.py:755`).
  Still present in dev @ c823fc5 (`dev transport/connection.py:915-923`, `dev const.py:60-65`,
  `dev registry.py:41-61`).
- **Proposed library change:** none required. Re-check after the refactor that `create_device` keeps
  `idle_disconnect` and that the `max()` rule still keeps a write's 30 s warm window.
- **Integration change and test:** `create_device(..., idle_disconnect=POLL_IDLE_DISCONNECT_SECONDS)`
  with 1.0 s (it must be greater than 0). This works at the floor; it is listed here only because the
  refactor touches connection timing. Test: assert `create_device` is called with `idle_disconnect`.
- **Verification:** confirmed by 1 verifier, lowered to low: the effect is transient and
  bleak-retry-connector backs off on out-of-slot errors.
- **Decision:** done in the integration: `POLL_IDLE_DISCONNECT_SECONDS` = 1.0 s, passed to
  `create_device` (works at 1.2.1 and 4f2c073). A modelled reconnect-storm check against both
  libraries' fakes found no storm: 1 connect per poll, per command burst and per self-test, no link
  closed mid-operation, none opened while idle. The one cost: a command 1-8 s after a poll now
  makes a connect instead of reusing the poll's link. Bench check in section 10.

### 6.2 `no-nonterminal-disconnect`

- **Severity / source:** unrated (the note calls it low). Review note `oos.performance.capture.3`.
- **Symptom in Home Assistant:** `capture_session` tests the targeted devices one after another
  ([custom_components/govee_ble_local/services.py:74-86](../custom_components/govee_ble_local/services.py#L74-L86)). Each test ends with restore writes, which keep that device's link warm
  for 30 s, so the next device's connect overlaps a slot that is still held.
- **Library behaviour:** `GoveeConnection.disconnect()` exists (`1.2.1 transport/connection.py:318-322`),
  but `Device` exposes only `connect()` and `stop()`, and `stop()` closes the device for good
  (`1.2.1 devices/device.py:255-262`). Still present in dev @ c823fc5
  (`dev transport/connection.py:369-373`, `dev devices/device.py:470-480`).
- **Proposed library API:** `Device.disconnect()`: drop the current link now (cancel the idle timer,
  under the connection lock) and keep the `Device` usable; the next operation reconnects.
- **Integration change and test:** call `await device.disconnect()` at the end of each self-test,
  after the restore, in `async_self_test_entry`. Test: `disconnect` is awaited after the restore.
- **Verification:** review note, not verified.
- **Decision:** open

### 6.3 `close-waits-for-inflight-op`

- **Severity / source:** unrated. Review note `oos.correctness.core.5`.
- **Symptom in Home Assistant:** an unload or reload while a poll is in flight waits for that
  operation, up to the 45 s operation timeout, because `device.stop()` waits on the connection lock
  ([custom_components/govee_ble_local/__init__.py:297-301](../custom_components/govee_ble_local/__init__.py#L297-L301)). The HA-side part is the confirmed finding
  `unload-leaves-inflight-poll-running` (register `device.stop` with `entry.async_on_unload` so HA
  cancels the poll tasks); it is not library-dependent.
- **Library behaviour:** `close()` sets `_closed` and then `disconnect()` takes the per-operation lock
  (`1.2.1 transport/connection.py:318-327`); an operation checks `_closed` only when it starts
  (`:329-331`). Still present in dev @ c823fc5 (`dev transport/connection.py:369-382`).
- **Proposed library change:** a cancel-aware `close()`: set `_closed`, cancel the task running the
  current operation (tracked in `_run`), then disconnect. The cancelled operation raises
  `GoveeBleConnectionClosed` (2.2).
- **Integration change and test:** none beyond `unload-leaves-inflight-poll-running`. Test: an unload
  during a blocked poll returns promptly.
- **Verification:** review note, not verified.
- **Decision:** open

### 6.4 `no-ha-stop-disconnect`

- **Severity / source:** unrated (no severity came with the remainder report). Review gap-pass finding
  `gap.4.no-ha-stop-disconnect`. The integration's lifecycle pass (B1) skipped it because it adds a
  new call site of the library's `Device.stop()` while the library is mid-refactor.
- **Symptom in Home Assistant:** Home Assistant does not unload config entries when it shuts down or
  restarts: `ConfigEntry.async_shutdown` only cancels a pending setup retry
  (`homeassistant/config_entries.py`). So the integration's only close, the `device.stop` on-unload
  job ([custom_components/govee_ble_local/__init__.py:145](../custom_components/govee_ble_local/__init__.py#L145) *(working tree)*; at `dc1c87f` the `await data.device.stop()` in
  `async_unload_entry`, [custom_components/govee_ble_local/__init__.py:301](../custom_components/govee_ble_local/__init__.py#L301)), never runs at shutdown. A GATT link that is open when Home
  Assistant stops is never closed by the integration: any device that had an operation in the last
  8 s, a write in the last 30 s, a poll in flight or a self-test running. Whether it is torn down
  promptly then depends on the adapter or proxy noticing that Home Assistant went away. Core
  `led_ble` closes its link from an `EVENT_HOMEASSISTANT_STOP` listener
  (`homeassistant/components/led_ble/__init__.py`, `_async_stop`).
- **Library behaviour:**
  - `Device.stop()` is `GoveeConnection.close()`: set `_closed`, then `disconnect()` under the
    connection lock (`1.2.1 devices/device.py:260-262`, `1.2.1 transport/connection.py:318-327`). The
    client teardown runs as its own task bounded by `DISCONNECT_TIMEOUT` = 5 s
    (`1.2.1 transport/connection.py:107`, `:491-508`). Still present in dev @ c823fc5
    (`dev devices/device.py:478-480`, `dev transport/connection.py:369-378`, `:601`; `dev const.py:80`).
  - It is terminal: a later operation raises `GoveeBleConnectionError("…: connection closed")`
    (`1.2.1 transport/connection.py:329-331`, `dev transport/connection.py:381-382`). That is what
    shutdown wants (nothing reconnects), but a poll or command still queued after it fails with that
    error.
  - A second call is harmless as written (`_disconnect_locked` returns when `_client` is None,
    `1.2.1 transport/connection.py:491-496`), so the stop listener and a later unload job could both
    call it. Neither version documents that `stop()` is idempotent.
  - It waits for the in-flight operation, up to the 45 s operation deadline, because `disconnect()`
    takes the connection lock (6.3). Home Assistant awaits stop-event listeners during its stop stage,
    so a wedged poll would hold shutdown up to 45 s (each device's listener runs in parallel).
  - The idle timer keeps the link open 8 s after any operation and 30 s after a write
    (`1.2.1 const.py:59-64`, `dev const.py:60-65`), so a stop soon after any command finds it open.
- **Proposed library change:** keep `Device.stop()` through the refactor and document its contract:
  idempotent, safe to call while an operation is in flight and from a shutdown handler, terminal
  (later I/O raises `GoveeBleConnectionClosed`, 2.2), and bounded. Make the bound real with the
  cancel-aware close in 6.3, so a stop during a wedged operation returns in about `DISCONNECT_TIMEOUT`
  instead of up to 45 s.
- **Integration change and test:**
  - In `async_setup_entry`, next to `entry.async_on_unload(device.stop)` ([custom_components/govee_ble_local/__init__.py:145](../custom_components/govee_ble_local/__init__.py#L145)
    *(working tree)*), add a `_async_stop(event)` that awaits `device.stop()` and register
    `entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))`, as
    core `led_ble` does. The `async_on_unload` wrapper removes the listener when the entry unloads
    first.
  - Decide whether the stop listener waits for a running self-test's restore, as `async_unload_entry`
    does ([custom_components/govee_ble_local/__init__.py:333-335](../custom_components/govee_ble_local/__init__.py#L333-L335) *(working tree)*, `SELF_TEST_UNLOAD_TIMEOUT_SECONDS` = 60 at
    [custom_components/govee_ble_local/const.py:77](../custom_components/govee_ble_local/const.py#L77) *(working tree)*). Waiting the full 60 s holds shutdown; not waiting
    leaves the device in a test state. A shorter bound in the stop listener is one option.
  - No floor raise: `Device.stop()` exists at 1.2.1.
  - Tests in `tests/test_init.py`: firing `EVENT_HOMEASSISTANT_STOP` awaits `device.stop` once; an
    unload before the stop event leaves `stop` awaited exactly once (the listener is gone); a stop
    during a self-test follows the chosen policy.
- **Verification:** gap-pass finding; no verifier record came with the remainder report.
- **Decision:** integration side done. The stop listener (`_async_close_on_stop` in
  `__init__.py`) waits up to `SELF_TEST_STOP_TIMEOUT_SECONDS` = 30 s for a running self-test's
  restore, then gives the close `STOP_CLOSE_TIMEOUT_SECONDS` = 5 s, so a busy link can't hold
  shutdown for 45 s before 6.3 lands. A self-test still running after 30 s is cut off: its restore
  fails with "connection closed" and is logged once. The library-side contract (document
  `stop()`, 6.3) is still open.

## 7. Capability / feature flags

### 7.1 `h61a8-segment-brightness-always-fails`

- **Severity / source:** medium. Review finding (also review note `oos.tests.tests-flow-light.0`).
- **Symptom in Home Assistant:** every H61A8 segment light shows a brightness slider, because
  `ColorMode.RGB` implies brightness ([custom_components/govee_ble_local/light.py:52-61](../custom_components/govee_ble_local/light.py#L52-L61), created at [custom_components/govee_ble_local/light.py:87-89](../custom_components/govee_ble_local/light.py#L87-L89)). Every
  brightness change raises "not supported" ([custom_components/govee_ble_local/light.py:353-359](../custom_components/govee_ble_local/light.py#L353-L359), mapped at
  [custom_components/govee_ble_local/entity.py:90-95](../custom_components/govee_ble_local/entity.py#L90-L95)). An area or group brightness call that reaches an enabled H61A8 segment
  reports an error. A brightness-only `turn_on` on a dark segment lights it with the colour frame and
  then fails.
- **Library behaviour:** `build.segment_brightness` raises for the `"h61a8"` scheme
  (`1.2.1 wire/build.py:170-173`), turned into `GoveeBleNotSupported` by `_build_or_unsupported`
  (`1.2.1 devices/device.py:431-452`). No capability says so: the H61A8 profile has SEGMENTS
  (`1.2.1 devices/profile.py:124-130`). Still present in dev @ c823fc5: the builder raises
  `GoveeBleNotSupported` directly (`dev wire/build.py:686-687`), and the profile comment says this gap
  is deliberately left to the builder, not to a capability (`dev devices/profile.py:61-69`). Dev's
  builder also refuses per-segment brightness for the `"h6006"`, `"h6188"` and `"h6061"` schemes
  (`dev wire/build.py:688-691`).
- **Proposed library API:** a public flag, for example `DeviceProfile.segment_brightness: bool`
  (or `Capability.SEGMENT_BRIGHTNESS`), False for every scheme whose builder refuses it.
- **Integration change and test:** when the flag is False, emulate brightness by scaling the colour:
  - base colour = `kwargs[ATTR_RGB_COLOR]`, else `_last_color`, else white; level =
    `kwargs[ATTR_BRIGHTNESS]`, else the current brightness, else 255;
  - send one `set_segment_rgb([i], scaled)`, keep the unscaled base in `_attr_rgb_color` /
    `_last_color`, and do not call `set_segment_brightness`;
  - while emulating, do not adopt the read-back (scaled) colour as the base, or each step compounds
    (50 % then 50 % gives 25 %).

  Until the flag ships, cache the first `GoveeBleNotSupported`. Tests: parametrised on the real
  H61A8 profile: brightness-only, colour plus brightness, and two brightness changes in a row with no
  compounding.
- **Verification:** confirmed by 1 verifier, who rejected the original fix because it compounds the
  dimming and sends two RGB frames.
- **Decision:** open

### 7.2 `h61a8-segment-lights-untested`

- **Severity / source:** medium. Review finding.
- **Symptom in Home Assistant:** none directly; the suite cannot catch 7.1. The only H61A8 light test
  ([tests/test_light.py:687-696](../tests/test_light.py#L687-L696)) asserts on the main light with segment lights disabled; every
  segment test runs on H60A6.
- **Library behaviour:** as 7.1.
- **Integration change and test:** an H61A8 test with `enabled_entry` asserting
  `supported_color_modes == ["rgb"]`, no kelvin range, and that `turn_on(rgb_color=RED, brightness=128)`
  succeeds with the expected H61A8 segment frame(s) and state. Land it with 7.1, or mark it
  `xfail(strict=True)` until then.
- **Verification:** confirmed by 1 verifier.
- **Decision:** open

### 7.3 `runtime-segment-count`

- **Severity / source:** unrated. Review gap-coverage note (out of scope of the gap pass).
- **Symptom in Home Assistant:** segment lights and the self-test use the static
  `device.profile.segments` ([custom_components/govee_ble_local/light.py:87-89](../custom_components/govee_ble_local/light.py#L87-L89), [custom_components/govee_ble_local/capture.py:447](../custom_components/govee_ble_local/capture.py#L447)). On an H6641 the read-back
  can cover fewer pieces than the 16 declared segments, so some segment lights never get state
  ([custom_components/govee_ble_local/GAPS.md:47-52](../custom_components/govee_ble_local/GAPS.md#L47-L52)).
- **Library behaviour:** changed in 1.3.0: `Device.segment_count` and `Device.set_segment_count()`
  exist (library `CHANGELOG.md:1129`; `dev devices/device.py:250-265`); not in 1.2.1. The library sets
  the runtime count only for profiles with `section_count_read` (H6062,
  `dev devices/device.py:1181-1192`). H6641's live IC read gives a piece count (IC clamped to 41-70,
  divided by 5, so 9-14 pieces; `dev devices/profile.py:455-478`, `dev devices/_readback.py:133-143`)
  that drives the read-back walk but is not adopted as `segment_count`.
- **Proposed library change:** decide whether H6641's IC-derived piece count is also its write/mask
  count. If so, adopt it as `segment_count` after the read, and document `segment_count` as the
  number of addressable segments callers should expose.
- **Integration change and test:** use `device.segment_count` where it exists (a `getattr` guard keeps
  the floor at 1.2.1, or raise the floor to 1.3.0 under CLAUDE.md rule 2). Entities are created at
  setup, so decide whether to create them after the first poll or for the profile maximum. Test on the
  newest leg: `set_segment_count(10)` gives 10 segment lights.
- **Verification:** review note, not verified. Needs hardware: H6641.
- **Decision:** open

## 8. Cloud parsing

### 8.1 `cloud-device-id-fixture-unrealistic`

- **Severity / source:** low. Review finding.
- **Symptom in Home Assistant:** none for users. The test [tests/test_config_flow.py:536-556](../tests/test_config_flow.py#L536-L556) builds
  a `CloudDevice` with `ble_mac=None` and a device id ending in the BLE MAC, which the library parser
  never produces, so it proves an unreachable fallback ([custom_components/govee_ble_local/config_flow.py:338-342](../custom_components/govee_ble_local/config_flow.py#L338-L342)). The realistic
  case, an id that carries the Wi-Fi MAC and no `deviceSettings.address`, is untested.
- **Library behaviour:** `GoveeCloudAccount._parse` fills `ble_mac` from the id's last 6 octets when
  `deviceSettings.address` is missing (`1.2.1 cloud/account.py:243-246`), and the docs say the id
  embeds the Wi-Fi MAC, one off the BLE MAC (`:60`, `:225-229`). Dev @ c823fc5: same fallback
  (`dev cloud/account.py:320-324`), now documented as best effort on `CloudDevice.ble_mac`
  (`:93-99`). `_parse` is still a private static method (`:289`).
- **Proposed library API (optional):** a public parser such as
  `CloudDevice.from_api(entry: Mapping[str, Any]) -> CloudDevice` (what `get_devices` uses), so
  consumers can build realistic fixtures without calling a private method.
- **Integration change and test:** build fixtures with the parser (or with `ble_mac` set to the id's
  last 6 octets). Retarget or drop `test_cloud_secret_device_id_fallback`. Add a case whose id ends
  in a MAC one off the advertised BLE address (the library example has Wi-Fi = BLE − 1) with no
  `deviceSettings.address`, and assert the intended outcome (`secret_not_found` today).
- **Verification:** confirmed by 1 verifier.
- **Decision:** open

### 8.2 `cloud-ble-mac-may-be-wifi-mac`

- **Severity / source:** unrated. Review notes `oos.correctness.platforms.3` and
  `oos.tests.tests-flow-light.3`; the related finding `cloud-ble-mac-wifi-fallback-misses` was
  refuted (nit).
- **Symptom in Home Assistant:** only if the account API returns a secret without
  `deviceSettings.address`: the derived MAC is then the Wi-Fi MAC and the cloud step returns
  `secret_not_found` ([custom_components/govee_ble_local/config_flow.py:338-343](../custom_components/govee_ble_local/config_flow.py#L338-L343)). The verifier found that the real API puts the
  secret and the BLE address together in `deviceSettings`, so the normal H5083 path matches, and
  found no evidence of the failing shape.
- **Library behaviour:** as 8.1; changed in dev only in the documentation
  (`dev cloud/account.py:93-99`, `:322`).
- **Proposed library change:** none unless a real response shows a secret without
  `deviceSettings.address`. Then derive `ble_mac` in the library (for example from `bleName` or
  `wifiMac`), not with a ±1 heuristic in the integration.
- **Integration change and test:** optional cleanup: `dev.ble_mac or ""` and a corrected comment at
  [custom_components/govee_ble_local/config_flow.py:339-340](../custom_components/govee_ble_local/config_flow.py#L339-L340).
- **Verification:** finding refuted by 1 verifier.
- **Decision:** open

### 8.3 `cloud-auth-vs-network-error`

- **Severity / source:** unrated. Review note `oos.tests.tests-flow-light.4`.
- **Symptom in Home Assistant:** a wrong Govee password and a network failure both show
  `cannot_connect` ([custom_components/govee_ble_local/config_flow.py:330-335](../custom_components/govee_ble_local/config_flow.py#L330-L335)).
- **Library behaviour:** at 1.2.1 every failure is a `GoveeCloudError`
  (`1.2.1 cloud/account.py:69-70`; login failure at `:193`); same in 1.3.0. Changed in dev:
  `GoveeCloudAuthError(GoveeCloudError)` for rejected credentials or token (`dev exceptions.py:73-78`,
  raised at `dev cloud/account.py:246` and `:265`), exported from `govee_ble_local`.
- **Proposed library change:** release it as in dev.
- **Integration change and test:** catch `GoveeCloudAuthError` before `GoveeCloudError` and show an
  `invalid_auth` error (new key in `strings.json`). This needs the floor raised to the first release
  that has it, or a guarded import. Tests: a login raising `GoveeCloudAuthError` shows
  `invalid_auth`; another `GoveeCloudError` still shows `cannot_connect`.
- **Verification:** review note, not verified.
- **Decision:** open

## 9. Library-internal bugs & performance

### 9.1 `active-scene-reverse-index-rebuilt`

- **Severity / source:** unrated; the notes call it negligible (microseconds per write). Review notes
  `oos.design.core.8`, `oos.performance.core.1`, `oos.correctness.platforms.1`,
  `oos.performance.platforms.3`, `oos.performance.capture.4`, and the gap-coverage note.
- **Symptom in Home Assistant:** none visible. The main light's `effect` property calls
  `Device.active_scene` on every state write ([custom_components/govee_ble_local/light.py:196](../custom_components/govee_ble_local/light.py#L196)).
- **Library behaviour:** at 1.2.1 `active_scene` (`1.2.1 devices/device.py:182-188`) calls
  `scene_names_by_code`, which rebuilds the code-to-name index from a fresh copy of the catalogue,
  up to 148 scenes (`1.2.1 scenes/__init__.py:98-114`). Same in 1.3.0. Changed in dev: cached per
  catalogue (`_NAMES_BY_CODE`, `dev scenes/__init__.py:189-205`).
- **Proposed library change:** release the dev cache.
- **Integration change and test:** none.
- **Verification:** review notes, not verified.
- **Decision:** open

### 9.2 `segment-brightness-readback-unclamped`

- **Severity / source:** unrated. Review note `oos.security.platforms.2`. The integration-side clamp is
  the separate finding `readback-brightness-unclamped` (confirmed, low, not library-dependent). The
  integration's entities/standards pass (B4) has fixed that finding and reported this library side
  back as an optional note: the integration fix is complete without it.
- **Symptom in Home Assistant:** if a device reports a segment brightness byte above 100, a segment or
  zone light publishes a brightness above 255 ([custom_components/govee_ble_local/light.py:314-317](../custom_components/govee_ble_local/light.py#L314-L317), [custom_components/govee_ble_local/light.py:399-400](../custom_components/govee_ble_local/light.py#L399-L400)). At the
  1.2.1 floor the main light does the same for a whole-device byte above 100
  ([custom_components/govee_ble_local/light.py:171-174](../custom_components/govee_ble_local/light.py#L171-L174)). All three went through an unclamped `pct_to_brightness` at `dc1c87f`.
  Fixed in the integration *(working tree)*, see below.
- **Library behaviour:** `Segment.brightness` is documented as 0..100 (`1.2.1 models.py:47`,
  `dev models.py:47`) but stored raw: `1.2.1 wire/reassemble.py:148-151`, `1.2.1 wire/parse.py:206`,
  `:231`; in dev every read-back path goes through `dev wire/parse.py:497-498` (the status burst via
  `dev wire/reassemble.py:454`). Whole-device brightness is raw at 1.2.1 when `brightness_max` is 100
  (`1.2.1 devices/device.py:283-285`) and clamped in dev (`dev devices/_fold.py:25-32`). Still
  present in dev @ c823fc5 for segments.
- **Proposed library change (optional):** clamp segment brightness to 0..100 when building a
  `Segment` from read-back, as dev already does for whole-device brightness, so the value matches the
  documented range at the source for every consumer. If a 1.2.x patch release is ever cut, the same
  clamp for whole-device brightness there.
- **Integration change and test:** done *(working tree)*: `pct_to_brightness` clamps the percent to
  0..100 before scaling ([custom_components/govee_ble_local/helpers.py:91-97](../custom_components/govee_ble_local/helpers.py#L91-L97)), and the main, segment and zone lights all use it
  ([custom_components/govee_ble_local/light.py:173](../custom_components/govee_ble_local/light.py#L173), [custom_components/govee_ble_local/light.py:310](../custom_components/govee_ble_local/light.py#L310), [custom_components/govee_ble_local/light.py:401](../custom_components/govee_ble_local/light.py#L401)). Test:
  `test_pct_to_brightness_clamps_out_of_range_readback` ([tests/test_helpers.py:54-57](../tests/test_helpers.py#L54-L57)). Keep the
  clamp even after a library fix, since the floor is 1.2.1. Nothing else to do here.
- **Verification:** review note, not verified.
- **Decision:** open

### 9.3 `parse-debug-log-unmasked-secret`

- **Severity / source:** unrated. Review note `oos.security.capture.3`.
- **Symptom in Home Assistant:** not in the self-test report (the record carries no address, so the
  ring handler drops it). With debug logging enabled for `govee_ble_local`, a secret-read reply
  (`aa b1`) that fails to parse would write its 8-byte secret into the Home Assistant log.
- **Library behaviour:** `_parse` logs `bytes(frame).hex()` at DEBUG with no masking
  (`1.2.1 wire/parse.py:41`, `dev wire/parse.py:72`), while the connection masks secret-carrying
  frames in its own capture (`1.2.1 transport/connection.py:210-232`, `:277-278`). Still present in
  dev @ c823fc5.
- **Proposed library change:** apply the connection's secret mask (`aa b1` replies, `33 b2` writes)
  before logging in `parse._parse`.
- **Integration change and test:** none.
- **Verification:** review note, not verified.
- **Decision:** open

## 10. Hardware checks needed

| Check | Devices | Decides | How |
|---|---|---|---|
| A colour, brightness or segment write sent while the whole device is off: dropped, stored, or wakes it | H6047, H6641, H61A8 (H60A6 whole fixture: wakes, per [custom_components/govee_ble_local/GAPS.md:39-41](../custom_components/govee_ble_local/GAPS.md#L39-L41)) | 1.2, 5.3 | library `tools/power_semantics_probe.py`, steps 5-6 |
| The same with one zone off: zone colour, zone kelvin or segment brightness, then zone on, then read the zone colour back | H60A6, H6047 | 1.2, 5.2, 5.3 | the probe has no zone step; add one |
| Whole-device CT, then segment or zone RGB: does the fixture leave CT; what does the status burst report per segment in CT mode | H60A6 | 1.3, 4.2 | live read of the 0xAC burst |
| What a CT segment reads back as through mechanism A direct (black, the tint, last RGB) | H6047, H6641 | 4.2, 4.4 | `aa a5 <group>` after `set_segment_color_temp` |
| Does the device answer the `aa a3` gradual query | H61A8, H6047 | 1.5 | poll with the dev library |
| Does `aa 01` answer reliably, and does the relay-bit mapping match | H5083 | 1.6 | poll with the dev library |
| Colour read-back piece count, 10 or 12 | H6047 | 1.1, 4.8 | read with the dev library; correct [custom_components/govee_ble_local/GAPS.md:29-31](../custom_components/govee_ble_local/GAPS.md#L29-L31) or the library profile |
| Live IC-derived piece count compared with the 16 declared segments, and whether writes follow it | H6641 | 7.3 | `0x40` read plus a mask test |
| The 1 s idle drop after a read (`POLL_IDLE_DISCONNECT_SECONDS`): still 1 connect per poll and per command burst; the latency of a command sent 2-5 s after a poll (it now reconnects first); peak ESPHome proxy slot use during the startup stagger, with no out-of-slot errors; the H5083 still polls and switches cleanly with the library dropping the link after 1 s, before its own ~10 s idle drop | H60A6, H5083, several devices on one ESPHome proxy | 6.1 | debug logging for `govee_ble_local` and `bleak_retry_connector`: count connect attempts per poll and per command; the proxy's free-slot count in its ESPHome log or Home Assistant's Bluetooth diagnostics during a restart |
