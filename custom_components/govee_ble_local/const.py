"""Constants for the Govee BLE Local integration.

Protocol constants, the scene catalog, per-model capabilities, encryption and
segment/zone layout all live in the ``govee_ble_local`` library and its device
classes now. Only Home-Assistant-integration-level constants remain here.
"""

DOMAIN = "govee_ble_local"

# 60s: with several lights sharing one adapter, each poll opens a BLE
# connection that briefly locks the device's single slot; a slower cadence
# reduces contention. These are near-static ceiling lights, so 60s is plenty.
POLL_INTERVAL_SECONDS = 60

# Maps a library zone name (GoveeDevice.zones[i].name) to an entity
# translation key. The v2 library names H60A6's zones "main" (upper ring) and
# "background" (lower panel); unknown zone names fall back to their raw name.
ZONE_TRANSLATION_KEYS: dict[str, str] = {
    "main": "upper_ring",
    "background": "lower_panel",
}
