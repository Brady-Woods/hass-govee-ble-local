"""Constants for the Govee BLE Local integration.

Protocol constants, the scene catalog, per-model capabilities, encryption and
segment/zone layout all live in the ``govee_ble_local`` library and its device
classes now. Only Home-Assistant-integration-level constants remain here.
"""

DOMAIN = "govee_ble_local"

# Config-entry key for the 8-byte secret key (hex) some devices (the smart-plug
# family) require before they accept commands.
CONF_SECRET = "secret"

# Rich state (brightness, colour, scene, segments, zones) needs a connection, so
# each poll briefly locks one of the adapter/proxy's scarce connection slots.
# on/off is now tracked PASSIVELY from advertisements (no slot), so this poll can
# be slow — it only reconciles the richer fields. Devices the advert reports as
# off aren't connected to at all.
POLL_INTERVAL_SECONDS = 120

# Ceiling for the coordinator's adaptive backoff. A device that repeatedly fails
# to connect (out of range / no free slot) launches a ~30-40s connection-retry
# storm on every poll; on a single adapter shared by ~18 devices that steals
# radio time from the healthy ones. After consecutive failures the coordinator
# doubles that device's poll interval up to this cap, resetting on the first
# success. on/off still updates live from advertisements regardless.
MAX_POLL_INTERVAL_SECONDS = 1800

# Maps a library zone name (GoveeDevice.zones[i].name) to an entity translation
# key. The library uses the device's own zone names (from the Govee app/cloud:
# mainLightToggle / backgroundLightToggle); unknown names fall back to the raw
# name via the switch's translation-key lookup.
ZONE_TRANSLATION_KEYS: dict[str, str] = {
    "main": "main_light",
    "background": "background_light",
}
