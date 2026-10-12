"""Constants for the Govee BLE Local integration.

Protocol constants, the scene catalog, per-model capabilities, encryption and
segment/zone layout all live in the ``govee_ble_local`` library and its device
classes now. Only Home-Assistant-integration-level constants remain here.
"""

DOMAIN = "govee_ble_local"

# Service that runs the in-HA device self-test and returns the captured session.
SERVICE_CAPTURE_SESSION = "capture_session"

# Config-entry key for the 8-byte secret key (hex) some devices (the smart-plug
# family) require before they accept commands.
CONF_SECRET = "secret"

# Rich state (brightness, colour, scene, segments, zones) needs a connection, so
# each poll briefly holds one of the adapter/proxy's scarce connection slots.
# on/off is also tracked PASSIVELY from advertisements (no slot), so this poll
# can be slow — it reconciles the richer fields and catches external changes.
POLL_INTERVAL_SECONDS = 120

# Ceiling for the coordinator's adaptive backoff. A device that repeatedly fails
# to connect (out of range / no free slot) launches a ~30-40s connection-retry
# storm on every poll; on a single adapter shared by ~18 devices that steals
# radio time from the healthy ones. After consecutive failures the coordinator
# doubles that device's poll interval up to this cap, resetting on the first
# success. on/off still updates live from advertisements regardless.
MAX_POLL_INTERVAL_SECONDS = 1800

# Integration-wide cap on simultaneous background polls. HA's Bluetooth manager
# already routes each connection to a scanner with a free slot; this just keeps
# our own polling from launching a connect storm (a single local adapter can
# only establish one LE connection at a time).
MAX_CONCURRENT_POLLS = 2

# Random jitter (seconds) added to the poll interval (base and backoff) so
# devices don't drift into lockstep over uptime. The crc32 stagger in
# __init__.py only spreads the very first poll.
POLL_JITTER_SECONDS = (0, 30)

# A device that has vanished from every scanner goes unavailable after this many
# consecutive failed polls. A device still advertising stays available.
UNAVAILABLE_AFTER_FAILURES = 3

# Publish the "last seen" diagnostic at most this often (advertisements arrive
# several times a second; one recorder row per advertisement is pure bloat).
LAST_SEEN_RESOLUTION_SECONDS = 60

# Maps a library zone name (Device.zones[i].name) to an entity translation
# key. The library uses the device's own zone names (from the Govee app/cloud:
# mainLightToggle / backgroundLightToggle); unknown names fall back to the raw
# name via the switch's translation-key lookup.
ZONE_TRANSLATION_KEYS: dict[str, str] = {
    "main": "main_light",
    "background": "background_light",
    "left": "left_bar",
    "right": "right_bar",
}
