"""Constants for the Govee BLE Local integration.

Protocol constants (UUIDs, PSK, opcodes), the scene catalog, per-model
capabilities, and the broken-scene flags now live in the ``govee_ble_local``
library and its device profiles. Only Home-Assistant-integration-level
constants remain here.
"""

DOMAIN = "govee_h60a6"

# 60s: with several lights sharing one adapter, each poll opens a BLE
# connection that briefly locks the device's single slot; a slower cadence
# reduces contention. These are near-static ceiling lights, so 60s is plenty.
POLL_INTERVAL_SECONDS = 60

# Maps a profile zone name to its (BLE zone index, entity translation key).
# Indices match govee_ble_local.const (ZONE_LOWER=0, ZONE_UPPER=1); kept as
# literals so this module stays import-light (the config-flow tests load it
# without the library installed).
ZONE_META: dict[str, tuple[int, str]] = {
    "upper": (1, "upper_ring"),
    "lower": (0, "lower_panel"),
}
