# Installation

`hass-govee-ble-local` is a custom Home Assistant integration. Its BLE protocol library
(`govee-ble-local`) is **not on PyPI yet** — Home Assistant installs it automatically at startup
from the Git URL in the integration's `manifest.json` (`requirements`). You just need to get the
integration's files into your Home Assistant `config/custom_components/` directory.

You also need a Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html)
in range of the device.

## Home Assistant OS / Supervised

**HACS (recommended):** HACS → ⋮ → **Custom repositories** → add this repo, category
**Integration** → install **Govee BLE Local** → restart Home Assistant.

**Manual:** copy `custom_components/govee_ble_local/` into `/config/custom_components/` and
restart.

Home Assistant auto-installs the library requirement on the next start.

## Home Assistant Container (Docker / Podman)

Copy the integration into the **mapped config directory on the host** (the host side of the
`/config` volume) — do **not** copy it into the running container with `docker cp` / `podman cp`.

```sh
# On the host, with <CONFIG> = the host path bind-mounted to the container's /config:
mkdir -p <CONFIG>/custom_components
rm -rf <CONFIG>/custom_components/govee_ble_local
cp -r custom_components/govee_ble_local <CONFIG>/custom_components/

# Match the ownership Home Assistant runs as (linuxserver images use PUID/PGID):
chown -R <PUID>:<PGID> <CONFIG>/custom_components/govee_ble_local

# Restart the container:
docker restart homeassistant   # or: podman restart homeassistant
```

### SELinux hosts (Fedora/RHEL/CoreOS, etc.)

If the host runs SELinux in **Enforcing** mode, files must carry a label the container can read.
Copying files in from the host can leave them with the wrong label, and Home Assistant will then
report **"Integration 'govee_ble_local' not found"** even though the files are present. Give them
the same context as an existing, working `/config` file:

```sh
chcon -R --reference=<CONFIG>/configuration.yaml <CONFIG>/custom_components/govee_ble_local
# verify (should match configuration.yaml's context, e.g. container_file_t:s0 with NO :cNNN,cNNN):
ls -Z <CONFIG>/custom_components/govee_ble_local
```

> **Why not `podman cp`?** Copying into a *running* container stamps the files with that
> container instance's private SELinux MCS categories. When the container is later recreated
> (e.g. an image auto-update), it runs under new categories and can no longer read those files.
> Writing into the mapped directory with a shared label (as above) survives recreates.

Likewise, never `pip install` the library into the container by hand — that lands in ephemeral
container storage and is wiped on the next recreate. Let Home Assistant install it from the
manifest requirement (it re-runs on every start).

## Upgrading

- **HACS:** open the integration in HACS → **Update** → restart Home Assistant.
- **Manual / container:** delete the old `govee_ble_local` folder, drop in the new release (repeat
  the ownership + SELinux relabel steps on a container host), and restart.
- After upgrading, confirm the integration loads (Settings → Devices & Services) and your
  entities are present.

## Verifying / troubleshooting

- **"Integration not found":** on a container host this is almost always a file-ownership or
  SELinux-label problem — re-run the `chown` + `chcon` steps and restart.
- **Library missing / import errors after a container image update:** restart Home Assistant so
  it reinstalls the requirement; ensure the container has network access to GitHub at boot.
- Download **Settings → Devices & Services → Govee BLE Local → ⋮ → Download diagnostics** for a
  redacted dump (recent warnings + last self-test capture).
