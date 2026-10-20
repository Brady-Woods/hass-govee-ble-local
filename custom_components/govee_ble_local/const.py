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
# True when the user chose to set up a model the library supports only as "modelled" (inferred
# from the app, not traced or hardware-verified); absent means False.
CONF_ALLOW_MODELLED = "allow_modelled"

# Colour-temperature range assumed when the library's profile doesn't give one.
DEFAULT_MIN_KELVIN = 2700
DEFAULT_MAX_KELVIN = 6500

# Rich state (brightness, colour, scene, segments, zones) needs a connection, so
# each poll briefly holds one of the adapter/proxy's scarce connection slots.
# on/off is also tracked PASSIVELY from advertisements (no slot), so this poll
# can be slow — it reconciles the richer fields and catches external changes.
# It is the fixed cadence of a device the advertisement policy below doesn't
# cover, and where a light that is on starts its backoff.
POLL_INTERVAL_SECONDS = 120

# Advertisement-driven polling (coordinator._async_policy_step). A Govee light's
# advertisement carries its on/off state and nothing else (no brightness,
# colour, colour temperature, scene, segments or zones), and the library folds
# that byte into its state (Device.ingest_advertisement). So while Home
# Assistant keeps hearing the byte, a light that is off needs no background
# poll (it shows nothing a poll would read, and turning it on shows in the
# advertisement), and neither does a relay plug, whose relay is its only state.
# A light that is on is still polled, with a backoff: a colour, brightness or
# scene changed in the Govee app or over Wi-Fi while it stays on reaches Home
# Assistant only through a poll.
#
# The on-backoff doubles from POLL_INTERVAL_SECONDS after each poll that found
# nothing new (120, 240, 480, then 900 s) and starts over on any change. 15 min,
# not the failure backoff's 30, bounds how stale such an outside change can get
# and still cuts a steady light's connections by about 85 % against 120 s.
ON_POLL_MAX_INTERVAL_SECONDS = 900

# A light turned on outside Home Assistant (the advertisement flipped to on) is
# polled once this long after, so the colour or scene it was turned on in, or
# picked right after, is read too. A newer on/off change restarts the wait.
ADVERT_FLIP_POLL_DELAY_SECONDS = 5

# How long after Home Assistant received it (BluetoothServiceInfoBleak.time) an
# advertised on/off byte counts as fresh. Background polls stop only for a
# device with a fresh byte; without one it keeps the fixed cadence. Just under
# the 195 s after which Home Assistant's Bluetooth stack treats a connectable
# device's advertisements as stale (habluetooth's
# CONNECTABLE_FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS, 195 in both 5.6.4
# and 7.1.2, the versions Home Assistant 2025.10.0 and 2026.10.0 pin; re-exported
# by homeassistant/components/bluetooth/const.py).
ADVERT_POWER_FRESH_SECONDS = 180

# Models that keep the fixed POLL_INTERVAL_SECONDS cadence (with its jitter and
# failure backoff) whatever their advertisements say. A static reading of the
# H60A6 firmware (not yet checked on the bench) found that a BLE disconnect
# ends a music or group mode started over Wi-Fi, so a poll's disconnect may end
# one; H60A1 is the same family and unchecked. The policy's turn-on poll would
# land seconds after the light comes on, just when such a session is started,
# so until a bench check settles it they keep today's behaviour exactly.
EXCLUDED_FROM_ADVERT_POLICY: frozenset[str] = frozenset({"H60A1", "H60A6"})

# Ceiling for the coordinator's adaptive backoff. A device that repeatedly fails
# to connect (out of range / no free slot) launches a ~30-40s connection-retry
# storm on every poll; on a single adapter shared by ~18 devices that steals
# radio time from the healthy ones. After consecutive failures the coordinator
# doubles that device's poll interval up to this cap, resetting on the first
# success. on/off still updates live from advertisements regardless. Failed
# polls back off this way under the advertisement policy too.
MAX_POLL_INTERVAL_SECONDS = 1800

# Integration-wide cap on simultaneous background polls. HA's Bluetooth manager
# routes each connection to a scanner with a free slot: a local adapter can only
# establish one LE connection at a time, but each ESPHome proxy has slots of its
# own, so two concurrent polls can both make progress on a mixed setup. The cap
# only keeps our own background polling from launching a connect storm.
MAX_CONCURRENT_POLLS = 2

# How long the library keeps a link open after an operation when no write is
# keeping it warm (create_device's idle_disconnect; the library default is
# 8 s). A poll gives its MAX_CONCURRENT_POLLS place back when update()
# returns, but its link, and the adapter or proxy connection slot it holds,
# stays open this long after: at 8 s the startup stagger left about three idle
# links behind the two active polls, all of an ESPHome proxy's default three
# slots. A short delay can't split calls that belong together: the library
# cancels the timer as each operation starts and re-arms it as it ends, and
# back-to-back calls (the reads of one update(), a zone light's 2-4 calls,
# the self-test's steps) start the next one well within 1 s. A write still
# keeps its link warm for 30 s (the library waits for the longer of the two),
# so only the tail after a read gets shorter; the cost is that a command 1-8 s
# after a poll opens a new link instead of reusing the poll's. It must be
# above 0: 0 or less never drops an idle link.
POLL_IDLE_DISCONNECT_SECONDS = 1.0

# Random jitter (seconds) added to the poll interval (base and backoff) so
# devices don't drift into lockstep over uptime. The crc32 stagger in
# __init__.py only spreads the very first poll.
POLL_JITTER_SECONDS = (0, 30)

# Spread (seconds) of the per-device stagger before the first background poll:
# FIRST_POLL_STAGGER_PER_DEVICE_SECONDS for each active device entry, at least
# FIRST_POLL_STAGGER_SECONDS and at most FIRST_POLL_STAGGER_MAX_SECONDS. Every
# entry sets up when Home Assistant starts, so with a fixed 30 s a larger install
# packed all its first connects (and the connection slots they hold) into the
# same half minute, and a command to any of those devices queued behind its poll.
# The cap stays a margin below the base interval, so a first poll never comes
# after the poll HA schedules next.
FIRST_POLL_STAGGER_SECONDS = 30
FIRST_POLL_STAGGER_PER_DEVICE_SECONDS = 5
FIRST_POLL_STAGGER_MAX_SECONDS = POLL_INTERVAL_SECONDS - 20

# A background poll that comes due while an entity command to the device is
# running, or within this long after one ended, waits until then: a poll then
# would take the device for its reads just when the user is likely to adjust
# again, and Home Assistant already shows what the command did. It ends a few
# seconds before the library drops a command's link (its 30 s ACTIVE_IDLE_DELAY
# after a write), so the held-off poll still finds the link up. A stream of
# commands holds a poll off at most POLL_HOLDOFF_MAX_SECONDS from when it came
# due; then it runs anyway.
POLL_HOLDOFF_AFTER_COMMAND_SECONDS = 25
POLL_HOLDOFF_MAX_SECONDS = POLL_INTERVAL_SECONDS

# A successful poll that took at least SLOW_POLL_SECONDS (connect, handshake and
# reads), or a device whose connectable advertisement is at or below
# WEAK_SIGNAL_RSSI, counts as slow: its background polls back off, doubling per
# slow poll up to SLOW_POLL_MAX_STEPS doublings (and MAX_POLL_INTERVAL_SECONDS),
# so its long connects don't keep holding a connection slot and the device that
# a command would wait behind. The steps set a floor, BASE x 2^steps (capped), on
# every interval: after a failure, a change or a policy step too. A fast poll at
# a usable signal resets them. A typical poll takes 1-5 s; a device at -92 dBm on
# the bench took 3-24 s. The time counts from the start of update(), so a poll
# that waited for the library's device lock behind another operation (a command
# or a self-test) counts that wait too. The connect itself is bounded by
# POLL_CONNECT_TIMEOUT_SECONDS. A poll that times out after SLOW_POLL_SECONDS or
# more (a connect that ran out of that budget, typically) adds a slow step too
# instead of a failure-backoff step; it still counts as a failed poll (toward
# availability and in the diagnostics).
SLOW_POLL_SECONDS = 10

# A background poll's connect gets a tighter budget than a command's (the
# library's defaults: 3 attempts per establishment, within its 45 s operation
# limit): a poll at a device that doesn't answer gives the device up after
# POLL_CONNECT_TIMEOUT_SECONDS (plus up to 5 s to tear the link down), instead
# of holding it, and a command queued behind it, for up to 45 s. The timeout is
# what bounds it: bleak-retry-connector retries transient failures (no free
# slot, device not found) up to 9 times whatever the attempts say. It bounds the
# connect only, not the reads. Commands keep the library's defaults.
POLL_CONNECT_ATTEMPTS = 1
POLL_CONNECT_TIMEOUT_SECONDS = 15
WEAK_SIGNAL_RSSI = -90
SLOW_POLL_MAX_STEPS = 3

# A zone light trusts its zone's "on" this long after the device confirmed it
# (the library's ``zone_power_confirmed``: the device reported the zone's power,
# or acked a write of it): then a colour or brightness change sends only that
# change, without the zone's power frame (as the Govee app does). Older, the
# zone may have been switched off elsewhere since (advertisements carry only the
# whole fixture's on/off), so it is switched on first.
ZONE_STATE_FRESH_SECONDS = 120

# A device that has vanished from every connectable scanner (through a Bluetooth
# proxy HA drops it from its history 195-525 s after its last advertisement;
# through a local adapter, at HA's next 5-minute sweep after BlueZ removes it)
# goes unavailable after this many consecutive failed (or skipped) polls, or
# once it has gone UNAVAILABLE_AFTER_UNHEARD_SECONDS without being heard or
# reached (a command that landed, or a poll that didn't fail once the device
# has been read back at least once), whichever comes first: a slow device's
# polls are spaced up to 8x apart, so counting them alone could take most of an
# hour. A device still advertising stays available.
UNAVAILABLE_AFTER_FAILURES = 3
UNAVAILABLE_AFTER_UNHEARD_SECONDS = UNAVAILABLE_AFTER_FAILURES * POLL_INTERVAL_SECONDS

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
