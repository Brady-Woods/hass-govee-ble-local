# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v0.12.1...v1.0.0
[0.12.1]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/Brady-Woods/hass-govee-ble-local/releases/tag/v0.12.0
