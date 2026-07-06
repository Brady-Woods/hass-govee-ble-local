"""Constants for the Govee BLE Local integration.

Protocol constants, the scene catalog, per-model capabilities, encryption and
segment/zone layout all live in the ``govee_ble_local`` library and its device
classes now. Only Home-Assistant-integration-level constants remain here.
"""

DOMAIN = "govee_ble_local"

# Config-entry key for the 8-byte secret key (hex) some devices (the smart-plug
# family) require before they accept commands.
CONF_SECRET = "secret"

# 60s: with several lights sharing one adapter, each poll opens a BLE
# connection that briefly locks the device's single slot; a slower cadence
# reduces contention. These are near-static ceiling lights, so 60s is plenty.
POLL_INTERVAL_SECONDS = 60

# Maps a library zone name (GoveeDevice.zones[i].name) to an entity translation
# key. The library uses the device's own zone names (from the Govee app/cloud:
# mainLightToggle / backgroundLightToggle); unknown names fall back to the raw
# name via the switch's translation-key lookup.
ZONE_TRANSLATION_KEYS: dict[str, str] = {
    "main": "main_light",
    "background": "background_light",
}
