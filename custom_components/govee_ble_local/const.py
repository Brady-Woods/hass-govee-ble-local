"""Constants for the Govee BLE Local integration.

Protocol constants, the scene catalog, per-model capabilities, encryption and
segment/zone layout all live in the ``govee_ble_local`` library and its device
classes now. Only Home-Assistant-integration-level constants remain here.
"""

DOMAIN = "govee_ble_local"
MANUFACTURER = "Govee"

# Service that runs the in-HA device self-test and returns the captured session.
SERVICE_CAPTURE_SESSION = "capture_session"

# Config-entry keys. The BLE address uses HA's CONF_ADDRESS ("address").
CONF_SKU = "sku"
# The 8-byte secret key (hex) some devices (the smart-plug family) require
# before they accept commands.
CONF_SECRET = "secret"  # noqa: S105 - a config key, not a secret
# Reconfigure-form checkbox: explicitly remove the stored secret (a blank secret
# field keeps the existing one).
CONF_CLEAR_SECRET = "clear_secret"  # noqa: S105 - a config key

# Colour-temperature range assumed when the library's profile doesn't give one.
DEFAULT_MIN_KELVIN = 2700
DEFAULT_MAX_KELVIN = 6500

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
# routes each connection to a scanner with a free slot: a local adapter can only
# establish one LE connection at a time, but each ESPHome proxy has slots of its
# own, so two concurrent polls can both make progress on a mixed setup. The cap
# only keeps our own background polling from launching a connect storm.
MAX_CONCURRENT_POLLS = 2

# Random jitter (seconds) added to the poll interval (base and backoff) so
# devices don't drift into lockstep over uptime. The crc32 stagger in
# __init__.py only spreads the very first poll.
POLL_JITTER_SECONDS = (0, 30)

# Spread (seconds) of the per-device stagger before the first background poll.
FIRST_POLL_STAGGER_SECONDS = 30

# A device that has vanished from every connectable scanner goes unavailable
# after this many consecutive failed (or skipped) polls. A device still
# advertising stays available.
UNAVAILABLE_AFTER_FAILURES = 3

# Refresh the "last seen" / RSSI diagnostics from HA's advertisement history this
# often (advertisements arrive several times a second; one recorder row per
# advertisement is bloat).
LAST_SEEN_RESOLUTION_SECONDS = 60

# Upper bound for each library call an entity command makes (connect + handshake
# + frames, and any wait for the library's per-device connection lock), so a
# call to a present-but-unconnectable device fails instead of hanging. It bounds
# each call, not a whole service call (a zone light's turn_on can make up to
# four), and not the time a command waits in Home Assistant's PARALLEL_UPDATES
# queue before it starts.
COMMAND_TIMEOUT_SECONDS = 60

# How long unloading an entry waits for a running self-test to finish (and put
# the device back the way it found it) before the BLE link is closed under it.
# Bounded so a self-test stuck on a bad link can't stall a reload indefinitely.
SELF_TEST_UNLOAD_TIMEOUT_SECONDS = 60

# When Home Assistant stops: how long the stop listener waits for a running
# self-test to finish and restore the device, then for the link to close (see
# _async_close_on_stop). HA gives its whole stop stage 100 s, shared with every
# other integration, so the self-test gets less than an unload gives it, and
# the close doesn't wait out an operation still using the link (up to the
# library's 45 s deadline). 5 s matches the library's own disconnect bound.
SELF_TEST_STOP_TIMEOUT_SECONDS = 30
STOP_CLOSE_TIMEOUT_SECONDS = 5

# Maps a library zone name (Device.zones[i].name) to an entity translation
# key. Zones with any other name use the raw zone name as the entity name.
ZONE_TRANSLATION_KEYS: dict[str, str] = {
    "main": "main_light",
    "background": "background_light",
    "left": "left_bar",
    "right": "right_bar",
}
