# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.1] - 2026-07-12

### Changed
- Bump the pinned `govee-ble-local` library requirement from `v1.0.0` to `v1.0.3`. Brings in
  three upstream fixes, all backward-compatible (no public API change; `scripts/check.sh`
  verified clean against the new pin — mypy `--strict`, 115 tests, 96.70% coverage):
  - **H6047 / H6641 segment colour read-back corrected.** Neither SKU actually answers the
    `0xAC` status burst; both are back to `readback="polled"` with a new, correctly-decoded
    direct per-group colour read (`mechanism_a_direct` — an interim fix had silently reused
    H61A8's decoder, which uses a different group size). H6641 also gains a live IC-count read
    instead of an approximation. Still not confirmed on real H6047/H6641 hardware — see
    `GAPS.md`.
  - **H60A6-style `0xAC` status reads now retry once on an empty parse** before leaving state
    stale, recovering a single dropped BLE notification mid-burst instead of costing a whole
    poll cycle. Field-tested by both the library (their changelog) and independently by us on
    `core` prior to this release: full state (brightness, all segments, both zones) recovered
    on retry in ~1.3s where it previously logged `state left stale`.

## [1.2.0] - 2026-07-12

Hardens the device self-test (`capture_session` service / "Run self-test" button) so it's a
genuine full integration test that unambiguously reports which device it ran against and fully
restores whatever it changed - two gaps found on review of the original implementation.

### Added
- The self-test report now includes the device's `address`, so results from multiple devices
  (the `capture_session` service can target several at once) are never ambiguous - previously
  two units of the same SKU would have been indistinguishable in the response.
- New coverage: segment brightness, the gradual/fade flag (H61A8), explicit zone-power-off, and
  whole-device power-off - each a distinct code path that wasn't previously exercised.
- Zone and segment colour changes are now verified against read-back state (tri-state, same as
  the existing checks), not just "did the command ACK".

### Fixed
- State restoration now covers everything the test actually changes, not just whole-fixture
  power/brightness/colour: every zone's power and colour, segment 0's colour/brightness, the
  originally active scene, and the gradual flag are all captured before the test and restored
  after - previously only the four whole-fixture fields were restored, silently leaving zones
  turned on, segments/zones recoloured, and any active scene cleared after every run.

## [1.1.0] - 2026-07-12

Slot-aware BLE connection scheduling, prompted by a live outage: two devices hit
`BleakOutOfConnectionSlotsError` after 10 connect attempts each when an ESPHome BLE proxy went
offline, leaving 12 devices contending for a shrunk connection-slot pool with zero cross-device
coordination beyond a one-time startup stagger.

### Added
- A domain-wide semaphore capping how many devices may be simultaneously mid-connect, sized from
  live Bluetooth connection-slot data (`homeassistant.components.bluetooth`), always reserving
  at least one slot for a manual connection (e.g. the Govee app) or another integration.
- The poll interval now dynamically lengthens when devices outnumber usable slots, and jitter is
  applied to every recurring interval and backoff-reset (not just the one-time initial stagger),
  so devices don't drift back into lockstep over uptime.
- `BleakOutOfConnectionSlotsError` (the whole pool is out of slots, not just this device) is now
  detected both proactively (before attempting a connect) and reactively, skipping the usual
  1-failure grace and backing off harder immediately, instead of retrying quickly into the same
  exhausted pool.
- A new **Connection source** diagnostic sensor showing which Bluetooth scanner (local adapter
  vs. a named ESPHome proxy) last saw each device - closes the blind spot that required manual
  log-digging to diagnose the outage above.
- Setup no longer blocks on a full connect+handshake for a device currently visible via BLE
  advertisement: it's marked ready immediately from passive state, and the real first poll runs
  as a staggered, slot-aware background task instead of serializing every device's handshake into
  the HA startup sequence. A device never seen advertising still requires a real connect before
  setup succeeds (`ConfigEntryNotReady` on failure), unchanged from before.

## [1.0.0] - 2026-07-11

First stable release, committing to Semantic Versioning. Verified against the
`govee-ble-local` library's `v1.0.0` release with no regressions (mypy `--strict`, full test
suite, ≥95% coverage).

### Changed
- **Pin the library requirement to `govee-ble-local @ …@v1.0.0`** (was unpinned `@master`), for
  reproducible installs now that the library has a stable tagged release — the pre-1.0
  `3.0.0.dev0` version collisions are gone.

### Added
- Placeholder brand assets under `brands/` (`icon.png`/`icon@2x.png` 256/512 square,
  `logo.png`/`logo@2x.png` 256×128 / 512×256) at the home-assistant/brands sizes, plus a
  stdlib generator (`brands/make_placeholders.py`) and a README. These are placeholders —
  replace with real artwork before submitting to home-assistant/brands; the `brands`
  quality-scale rule stays `todo` until real assets are accepted upstream.

## [0.12.1] - 2026-07-11

### Added
- `INSTALL.md` with a containerized-Home-Assistant (Docker/Podman) + SELinux walkthrough.
- This `CHANGELOG.md`.
- README: install/upgrade instructions (the library dependency is installed by HA from Git, as
  it is not on PyPI yet), an "Entities & services" section, and the curated real-hardware test
  device list.

### Changed
- Quality-scale self-assessment (`quality_scale.yaml`) updated to reflect the `capture_session`
  service (the `action-setup` / `docs-actions` rules are no longer exempt) and documented the
  path to the remaining Platinum items.

### Fixed
- Documented the standard, recreate-durable install method after a container auto-update
  (`:latest`) broke a previous manual deployment: files belong in the mapped `config` directory
  with a shared SELinux label — never `podman cp` into a running container, and never hand-install
  the library into ephemeral container storage.

## [0.12.0] - 2026-07-10

Migration to the `govee-ble-local` v3 library and a large feature expansion.

### Added
- Per-**segment** light entities (RGB + color temperature) for segment-addressable fixtures,
  registered disabled-by-default.
- Per-**zone** light entities (RGB + independent color temperature) for colour-controllable
  zones; on/off-only zones remain switches.
- Smart-plug power **read-back** (state reflected from the device, not just optimistic).
- Diagnostics: **Last seen** and **Last connected** timestamp sensors; the connectivity binary
  sensor now reflects **advertisement presence** rather than connect-poll success.
- Bluetooth MAC surfaced on the device registry entry.
- Diagnostic **capture**: an always-on WARNING+ ring buffer plus a full-capability device
  **self-test** — a `capture_session` service (returns the captured BLE session) and a
  "Run self-test" button.
- Device-info (hardware/firmware/serial/MAC) surfaced from the library's read-back.

### Changed
- Migrated to the v3 library API (`Device` / `DeviceProfile` / `device_profile_for`).
- Main light reports `EFFECT_OFF` when idle and lists it, so effects can be cleared.
- Fixed / added entity icons and names; on/off tracked passively from advertisements between
  polls.

[Unreleased]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v0.12.1...v1.0.0
[0.12.1]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/Brady-Woods/hass-govee-ble-local/releases/tag/v0.12.0
