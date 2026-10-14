# hass-govee-ble-local: rules for Claude

## Home Assistant dependency rules (never break these)

This integration installs into the same Python environment as Home Assistant itself. A
requirement that conflicts with Home Assistant's pins stops the integration from loading for every
user. Integration 1.4.0 did that: its exact pin `govee-ble-local==1.2.0` dragged in
`cryptography<49`, and Home Assistant 2026.10 requires `cryptography==50.0.1`
(Brady-Woods/govee-ble-local#8).

1. **The only manifest requirement is `govee-ble-local`, as a semver range:
   `govee-ble-local>=<floor>,<<next major>`** (e.g. `>=1.2.1,<2`). Never an exact `==` pin: it
   blocks library fixes, and drags the library's own dependency specs into the user's install.
   `tests/test_manifest_requirements.py` enforces the shape.
2. **Raise the floor only when the integration starts using something a newer library version
   adds.** Home Assistant won't upgrade an installed library that still satisfies the range, so
   users can stay on the floor indefinitely. CI tests both ends (`LIB_VERSION=floor` and
   `newest` in `scripts/check.sh`); both must pass.
3. **Move the upper bound only for a new library major,** after the integration has been
   updated for that major's API changes.
4. **Never list Home Assistant's own dependencies in `manifest.json`** (`bleak`,
   `bleak-retry-connector`, `cryptography`, …). They come from Home Assistant; hassfest rejects
   them.
5. **Before releasing, prove the requirement installs alongside Home Assistant.** Resolve it
   against the current stable and the `dev` `package_constraints.txt`, and against the oldest
   supported Home Assistant (`hacs.json` → `homeassistant`):

   ```bash
   gh api -H "Accept: application/vnd.github.raw" \
     "repos/home-assistant/core/contents/homeassistant/package_constraints.txt?ref=<tag or dev>" \
     > ha_constraints.txt
   python -m pip install --dry-run -c ha_constraints.txt "$(python -c 'import json;print(json.load(open("custom_components/govee_ble_local/manifest.json"))["requirements"][0])')"
   ```

   A `ResolutionImpossible` means the release would leave users with "Not loaded". Don't ship it.

The library has the matching rules in `govee-ble-local/CLAUDE.md`: its own runtime dependencies
carry a `>=` floor only.
