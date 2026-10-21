# Changelog

All notable changes to this integration are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A segment light follows its zone's power** (H60A6, H6047). With the fixture on but the
  segment's zone off (the H60A6's ring, an H6047 bar), a segment light showed on, and turning it
  on sent the colour to a segment that stayed dark: with the fixture on, a colour frame lights no
  zone. Now the segment shows off while its zone is known off, and turning it on switches that
  zone on first, then sends any colour, as a zone light does. The zone's other segments light too,
  in the colours they kept. A zone not confirmed lit in the last 2 minutes (it may have been
  switched off in the Govee app) also gets its power first; that frame does nothing if it is on.
  This matches how a segment light already follows the whole fixture's power.
- **A turn-off of a light already off waits only for its own burst.** It waited while a command
  to any other device ran, so a lone turn-off (a script step) could wait the full 10 s behind
  unrelated commands that kept starting: another automation, a slider elsewhere. Now it waits only
  for the commands that were running as it started, and a device it waits for is done once no
  command to it runs. A command that starts later doesn't hold it back.
- **A poll released at the end of a burst no longer runs in the middle of the next one.** Polls
  released as commands to several devices end wait for one of the two poll places (and at the
  connect gate). One still waiting there as a new burst starts, or as a command to its own device
  starts, now looks again once it has its place: it is held, reads nothing, and runs once those
  commands are done (or at the 2-minute cap). The first poll after setup and the return poll stay
  held apart, as before. A manual refresh, and a poll the cap released, still run anyway.
- Tests: a context without a user is checked to be no person's without the user store being
  asked. Home Assistant answers "no user" for no id today, so dropping that check in
  `helpers.async_context_is_person` passed every test. No change to the integration itself.

## [1.6.0] - 2026-10-21

Requires `govee-ble-local` 1.6.0 or newer (any 1.x) **from PyPI** and Home Assistant 2026.10+.

### Added
- **Lights already off go last in a burst:** when commands go to several devices at once ("turn off
  all the lights", an area, a light group), turning off a light or smart plug that is already off
  waits until the other devices' commands are done, or 10 s at most, and then goes out. The devices
  that actually change get the scarce Bluetooth connection slots first. In real nightly "all off"
  bursts about half the lights were already off, and the last light to go out waited for the slot of
  one of them. The turn-off is still sent, so a light wrongly believed off still goes off. Only the
  device's own report counts: an advertised on/off, an acknowledged turn-off, or a poll once a
  poll's state read has answered since setup, from the last 3 minutes (with `govee-ble-local` 1.6.0
  a later poll whose reads went unanswered also counts, as the library doesn't say; such a turn-off
  then waits up to 10 s, then goes last at the gate). An unknown, assumed or older state goes out at
  once, and so does a command to a single device. Lights already off don't wait for each other: with
  every light already off, all go out at once, and so does the turn-off of a device whose Bluetooth
  link is already up (waiting would save no connection slot). Another call to the same light while
  its turn-off waits (a motion automation turning it back on, say) sends the turn-off at once, so
  the newer call isn't held up behind it; for a zone's turn-off, a call to any of the device's
  lights does. A waiting turn-off counts as one of the commands to several devices (every device's
  polls wait for it), and at the connect gate it goes after the other commands. A turn-off still
  waiting when Home Assistant stops or the device's entry unloads is dropped. This covers the
  whole-fixture light, the plug's power switch and, while the whole fixture is already off, a zone
  light's or zone switch's turn-off.

### Changed
- **The README lists every supported model:** 98 with `govee-ble-local` 1.6.0, plus 6 you can
  opt in to, each with its type, features and whether it is in the real-hardware test set. The
  list is generated from the library (`scripts/gen_supported_models.py`), and a test fails when
  it no longer matches. Before, the README showed only the test set and a plug family. The test
  set's table now gives the H61A8 its colour temperature (added in 1.5.0) and lists only the
  H5083 among the plugs, the one plug tested here.
- **Requires `govee-ble-local` 1.6.0 or newer** (`>=1.6.0,<2`, was `>=1.5.0,<2`): the link release
  below uses `Device.release()`, which 1.6.0 adds. Home Assistant installs the new library itself
  on the next restart.
- **Only a person's command keeps the Bluetooth link open.** After a command the library keeps the
  light's Bluetooth link open for 30 s, so a second tap or a slider drag doesn't have to reconnect
  (from `govee-ble-local` 1.6.0 its heartbeat also stops the light ending the link sooner). That now
  happens only for a command from a person using Home Assistant: the dashboard, a more-info dialog,
  the companion app. Half a second after a light's last command, the integration releases its link
  instead when no command in that time came from a person (a voice satellite, an automation,
  HomeKit, Alexa or Google Assistant through Home Assistant Cloud, a VoIP phone), or when another
  device was commanded at about the same time ("turn off all the lights", an area, a light group, a
  script's steps, from the dashboard too). The light's connection slot then frees for the others: an
  ESPHome proxy has three, and held links made the last lights of "turn off all the lights" wait
  10-30 s for one. A light and its zone lights in one call still share one link. The cost is a
  reconnect for a command that follows a released one (about 1-2 s, more through a busy proxy).
  Typed or spoken Assist in the app, a client using your own long-lived token (a standalone
  Node-RED, say) and Alexa or Google Assistant linked without Home Assistant Cloud act as a
  logged-in user, so they keep the link as the dashboard does. Add-ons that reach Home Assistant
  through the Supervisor, such as the Node-RED and AppDaemon add-ons as set up by default, act as
  its system user, so their commands release the link as an automation's do. A release that falls
  while a poll is reading the device waits for the poll to end, so the poll's remaining reads don't
  have to connect again. While a link is being released, the connect gate (below) counts it as down,
  so a new command to that light connects through the queue like any other. Once a link is released,
  polls no longer count on it: a light that has stopped advertising is skipped as absent and goes
  unavailable, as after an idle disconnect, instead of spending a connect on it. Turn on
  `custom_components.govee_ble_local.coordinator` debug logging for a line per release and its
  reason.
- **Mass commands no longer overbook a Bluetooth proxy.** "Turn off all the lights" started every
  light's connect within milliseconds, and Home Assistant's Bluetooth stack handed a 3-slot ESPHome
  proxy five of them: it trusts the free-slot count the proxy last reported. The proxy refused the
  extra ones, which then went down whatever path was left, even a local adapter at -96 dBm that
  waited out a 20 s timeout: most lights were done in 5-7 s, the last at 27-34 s. Each command's
  device call, and each background poll, now waits at an integration-wide connect gate until the
  radio Home Assistant would pick has room: a free slot once our connects it hasn't reported yet
  are counted, and for a proxy no other connect in progress through it (a proxy can jam on
  concurrent connects; a local adapter connects several devices side by side). That path must also
  suit the device (at or above -85 dBm, not failing again and again); if it doesn't, the device
  waits only while a better path is busy and could be picked once free. A device heard well only
  through the proxy waits for it and goes first. Commands go before polls. A radio that has heard
  no advertisement for 10 s while not connecting normally stops counting as a usable path for 5
  minutes, so it doesn't draw the devices whose better path is only busy: in a measured burst a
  software proxy reporting every slot free hung both connects sent to it for 40 s, deaf all the
  while. For 3 s after one of these calls through a radio ends, that radio isn't judged deaf (a
  proxy pauses listening while it connects); one that failed has its radio judged once those 3 s
  are over, and one that worked queues no such check. A call waits at most 12 s, then
  goes ahead as before, one per radio at a time and not to a radio just given another connect.
  When a device's entry unloads or Home Assistant stops, its commands still waiting go ahead at
  once, so they reach the device before its link closes; a poll still waiting skips its read. If
  the gate can't read the Bluetooth scanners it logs the error and lets the calls through, and a
  turn-off waiting for the other devices ("Lights already off go last", above) then goes out as
  if its link were down. It can't force Home Assistant's path choice, only predict it, and it
  doesn't cover the self-test. Turn on
  `custom_components.govee_ble_local.connect_gate` debug logging for one line per call: how long it
  waited, the radio expected, how long the call took after.
- **No background polls while commands go to several devices at once.** While Home Assistant
  sends commands to two or more Govee devices at the same time (a light group, an area, "turn off
  all the lights"), every device's background poll that comes due waits until 25 s after the last
  of those commands ends, instead of only the commanded devices' polls. A poll connects to its
  device, so it would take one of the few Bluetooth connection slots those commands need (an
  ESPHome proxy has three). A poll already running when the commands start still runs to its end.
  A command that starts during those 25 s, to any device, starts them again when it ends; outside
  that, a command to one device still holds only that device's polls. A poll still waits at most
  2 minutes. The poll 5 s after a light is turned on outside Home Assistant, and the one when a
  device is back in reach, now wait for commands the same way, also for a command to that device
  itself; the one when a device is back in reach still runs if it came back switched off.
- **Fewer frames when a fixture and its zones are turned off together.** A zone light's or zone
  switch's turn-off sends nothing when the fixture isn't known to be on, no other command to the
  device is running, and the device itself confirmed that zone off less than 5 seconds earlier:
  by acknowledging the fixture's power-off (which switches every zone off), in a read-back, or by
  acknowledging that zone's own turn-off. Before, "turn off all the lights" sent an H60A6 a
  zone-off for each of its two zones after the power-off, and an H6047 two bar switches. The
  saving is small: about 0.1 s of Bluetooth link time per H60A6 and 0.3 s per H6047 on each such
  command. A zone-off still goes out for a zone turned off before its fixture in the same call
  (Home Assistant runs a call that lists several lights in no fixed order), in a fixture known to
  be on, confirmed off longer ago, or shown off only by an advertisement or an assumption. What the
  next turn-on lights doesn't change: on both models it relights every zone either way (checked
  on the devices).
- Tests: the stand-in for the library's connection (`tests/conftest.py` `FakeConnection`) gains
  what `govee-ble-local` 1.6.0's `Device` uses (its BLE heartbeat and `Device.release()`):
  `hold_heartbeat()`, `disconnect(*, keep=None) -> bool`, `is_connected` and `link_number`, so
  the suite passes against 1.6.0, the newest library. No change to the integration itself.
- Removed the code that only existed for `govee-ble-local` before 1.5.0 or Home Assistant before
  2026.10, which 1.5.0 already requires. The config flow catches the library's
  `GoveeCloudAuthError` directly and imports `probatio` instead of Home Assistant's `voluptuous`
  alias; segment lights read the library's `Device.segment_count`; the advertisement policy no
  longer checks for the app-matching advertisement parser, which every supported library has.
  The tests lose their library-version gates and skips, and a new one pins the window after a
  command or read in which a repeated advertisement isn't offered to the library again. Nothing
  changes on supported versions.
- Tests and type checks run against Home Assistant 2026.10.2 (`homeassistant==2026.10.2`,
  `pytest-homeassistant-custom-component==0.13.373`), which pins it exactly, so the two move
  together. The minimum Home Assistant stays 2026.10.0.

### Fixed
- **"Turn off all the lights" no longer switches an H60A6 or H6047 back on when its segment
  lights are enabled.** Each segment light's turn_off blacked its segment out with a colour frame,
  sent after the fixture's power-off (13 on an H60A6, 10 on an H6047). On these two models a colour
  frame switches a fixture that is off back on, lighting every zone, while Home Assistant showed
  everything off (checked on both models: the fixture came back on every time). A segment light
  of an H60A6 or H6047 that isn't known to be on now sends nothing on turn_off: the segment is
  dark already, keeps its colour, and lights again with the fixture. With the fixture on, turn_off
  still blacks the segment out. Segment lights are disabled by default.
- **Segment lights follow the fixture's power.** A segment light now shows off while its fixture
  is known to be off; before, it kept showing on in the colour the segment kept. Turning a segment
  light on while its fixture isn't known to be on switches the fixture on first, which lights its
  other segments in the colours they kept, then sets the segment. Before, a plain turn_on sent
  nothing and the fixture stayed dark. This applies to every model with segment lights.

## [1.5.0] - 2026-10-20

Requires `govee-ble-local` 1.5.0 or newer (any 1.x) **from PyPI** and Home Assistant 2026.10+.

### Added
- **Opt-in setup for unverified models:** a model whose support the library infers from the Govee
  app, rather than checking it against the app's own controls, and that hasn't been tested on a
  real device, is no longer simply refused. Discovery and manual setup ask first, say plainly that
  its support is inferred and untested, and set it up only if you accept. The choice is stored in
  the device's entry, and deleting the entry withdraws it. Fully unsupported models are still not
  offered. Existing entries set up as before.
- **H61A8 colour temperature:** the library gives the H61A8 colour temperature and per-segment
  brightness (since `govee-ble-local` 1.4.0). The main and segment lights offer colour temperature
  over the range the library gives that unit (2700-6500 K, widening to 2000-9000 K on hardware
  below 3.08.01 once its version is read; see Fixed), and the self-test runs the
  colour-temperature steps.
- **Wrong Govee password shown as such:** when the library reports that the account rejected the
  email or password (`GoveeCloudAuthError`, since `govee-ble-local` 1.4.0), the cloud secret step
  says so (`invalid_auth`) instead of the generic "could not log in or reach the Govee cloud".
- `capture_session`: every entry for a device that couldn't be tested now carries a stable
  `reason` code next to the `error` message (`not_loaded`, `self_test_device_not_found`,
  `self_test_busy`, `secret_rejected`, or `unknown`), so an automation can branch on it. The
  `error` text is unchanged.

### Fixed
- **A broken `govee-ble-local` install no longer stops lights from setting up.** From 1.5.0 the
  library raises for every model when its bundled scene store is missing or corrupt (1.4.x just
  had no effects), which made every device with scenes fail setup. Lights now set up and work
  without effects. Home Assistant logs one error naming the installed library and how to fix it
  on Home Assistant OS, in a container or in a Python virtual environment. The diagnostics show the
  error instead of a scene count, and the self-test skips its scene steps. Nothing reads the broken
  store again on Home Assistant's event loop.
- **Discovery checks support at the device's advertised protocol:** the config flow now asks the
  library whether a model is supported at the protocol version ("pact") its advertisement carries,
  the same way setup routes it. Some models are served only at certain protocol versions; before,
  such a device could be offered and then fail setup with "model not supported". Before the entry is
  created, the device is checked again at the advertisement setup will use, in case that one carries
  another protocol version (or the first had none). A model that is supported, but not at the
  protocol version its device advertises at setup, now retries setup instead of failing for good.
- **Colour-temperature range follows the device:** lights re-read the range from the library on
  every update instead of only at setup. With `govee-ble-local` 1.4.0 an H61A8's range depends on
  its hardware version, which the first poll reads after the lights are built. An older unit
  (hardware below 3.08.01) supports 2000-9000 K, but its lights reported 2700-6500 K on every
  start, so their state, their entity registry entries and the colour-temperature slider stopped
  there; a restart didn't help. Commands with a temperature outside that range already reached the
  device. A range that depends on the protocol version a device advertises now shows as soon as an
  advertisement carries it, instead of at the next poll, which can be 15 minutes away.
- **The effect list follows the device:** lights re-read the scenes the library offers on every
  update, and check a requested scene against them before sending anything. In `govee-ble-local`
  1.5.0 no model's scene list depends on its firmware or protocol version (the H61A8 offers the
  same scenes everywhere), so the list stays as it is; this is ready for later library releases
  whose scene catalogs vary.
- **Bluetooth discovery follows the library's own discovery lists** (`govee-ble-local` 1.4.0): it
  also matches Govee's encrypted broadcast headers (manufacturer IDs 0x8841 and 0x8842) and plain
  `GV` + digit names (`GV6047…` is an H6047; Home Assistant can't match the library's two-letter
  `GV` prefix on its own, so it is matched as `GV0*` … `GV9*`).
  Matching isn't support: a device the installed library doesn't support, such as a Govee sensor,
  is still not offered (its discovery aborts as "not supported"), and new models work only once a
  library release admits them.
- The config flow uses `profile_for()` (in the library since 1.2.1) instead of
  `device_profile_for()`, which `govee-ble-local` 1.5.0 deprecates.
- Self-test reports for secret-gated devices drop every frame line without a plaintext view. Newer
  libraries label frames captured before the session is ready `withheld` instead of `cipher`, and
  those lines were passing through.
- **Last seen and RSSI froze** for a device that keeps advertising the same thing (a light left in
  one state): Home Assistant only calls the advertisement callback when an advertisement changes,
  so these diagnostics now come from Home Assistant's advertisement history every minute. After a
  restart, last seen reflects when the device was actually heard instead of the startup time.
- **Entities stayed available forever** when polling had stopped (polling disabled in the entry's
  system options, or halted by a rejected secret) and the device then left range. They now go
  unavailable as soon as no connectable scanner sees the device.
- Advertisements from passive (non-connectable) Bluetooth scanners now update on/off and presence
  too; only connectable ones are used for connecting.
- After a run of failed polls, a command that reaches the device brings polling back to the normal
  cadence (once per failure streak; for a slow device, its own slower one) instead of waiting out a
  backoff of up to 30 minutes.
- **Self-test:** fails straight away with a clear error when no connectable scanner sees the device,
  instead of timing out step by step while blocking every other device's self-test. Reloading or
  unloading the entry while a self-test runs now waits (up to 60 s) for the test to restore the
  device before the connection is closed.
- **Self-test frame capture was silently empty** when `govee_ble_local` had a level set through
  Home Assistant's logger (`logger:` in YAML, `logger.set_level`, or the "Enable debug logging"
  toggle once used). A child logger you made chattier keeps its debug output during a capture,
  and restore failures are now logged even when the test ends with an unexpected error.
- **Diagnostics could expose the Wi-Fi MAC and serial number** through the self-test frame hex
  (reversed byte order, or an identifier split across two frames). Hex runs in the stored self-test
  report and recent warnings are now redacted, and so is the entry title (the advertised name ends
  in MAC bytes). Raw frames stay available through the admin-only `capture_session` action.
- A device that reports a brightness above 100 % no longer publishes a brightness outside 0-255.
- A pasted secret with an embedded tab, newline or `-` separators is cleaned up instead of being
  stored as typed.
- Fewer needless state writes: the poll-interval sensor shows the nominal interval (it changed to a
  new random value on every poll), and a device dropping out of the scanner history no longer
  pushes to every entity twice.
- With every entity of a device disabled, the device-registry sync no longer keeps the device
  polled.
- **The Bluetooth connection is now closed when Home Assistant stops or restarts.** Home Assistant
  doesn't unload integrations at shutdown, so a connection still open (a command in the last 30 s,
  or one still running) was left for the adapter or proxy to notice. A running self-test gets up
  to 30 s to finish and restore the device first (an unload gives it 60 s); one that takes longer
  is cut off and leaves the device in its test state, with the restore failure logged. Closing a
  connection still in use doesn't hold up the shutdown for more than 5 s.
- **A finished poll holds its Bluetooth connection slot for 1 s instead of 8 s.** The poll limit
  counted running polls, not open connections, so during the startup stagger the idle connections
  left behind could fill an ESPHome proxy's three slots and delay or fail other polls and commands.
  After a command the connection still stays open for 30 s for quick follow-up changes; a command
  sent 1-8 s after a poll now opens a new connection instead of reusing the poll's.

### Changed
- **Requires Home Assistant 2026.10.0 or newer** (`hacs.json`, was 2025.10.0), the version every
  test and check runs on.
- **Requires `govee-ble-local` 1.5.0 or newer** (`>=1.5.0,<2`, was `>=1.2.1,<2`): the
  advertised-protocol support check and the opt-in for unverified models use what 1.5.0 adds. Home
  Assistant installs the new library itself on the next restart.
- **Far fewer background connections, driven by what the advertisements show.** A Govee light's
  advertisement carries its on/off state (nothing else), so while Home Assistant has heard that
  byte in the last 3 minutes it no longer polls every 2 minutes regardless:
  - a light that is **off** isn't polled at all, and neither is a **smart plug** (H5083 family),
    whose relay is its only state;
  - a light turned **on outside Home Assistant** (Govee app or a remote) is polled once,
    5 s later, to read the colour or scene it came on in;
  - a light that is **on** is polled after 2 minutes, then backs off (4, 8, then every 15 minutes)
    while nothing changes, and returns to 2 minutes when anything does: a poll that reads something
    new, an advertised on/off change, or a command from Home Assistant. A colour, brightness or
    scene changed in the Govee app or over Wi-Fi while the light stays on can take up to 15 minutes
    to show (it was 2).

  An on/off change made outside Home Assistant within seconds of a Home Assistant command or poll
  (which the library ignores for 15 s) is taken from the repeated advertisement within about a
  minute. A device whose on/off broadcast isn't heard (an H6006 seen only by a passive scanner), or
  hasn't been for 3 minutes, or that no connectable Bluetooth adapter or proxy hears, is polled
  every 2 minutes as before, and so is every H60A6 and H60A1 (a Bluetooth disconnect may end a music
  or group mode started over Wi-Fi; not yet checked on hardware). Failed polls still back off to 30
  minutes (a poll that timed out slowly, to 16; see below). It uses the library's advertisement
  parser, which matches the Govee app's. The poll interval sensor shows unknown while a device has
  no background polls, and its diagnostics name the step (`poll_policy`).
  A light or plug with no background polls goes unavailable as soon as no connectable Bluetooth
  adapter or proxy hears it, instead of after three missed polls.
- **Zone lights (H60A6 main panel and ring, H6047 bars) respond sooner, in the Govee app's
  order.** Changing the colour, colour temperature or brightness of a zone that is lit now sends
  only that change, as the app does, instead of re-sending the zone's power after it; on an H6047
  that also skips reading the other bar first. A zone that is off, or whose "on" the device hasn't
  confirmed in the last 2 minutes (by reporting that zone's power, or acking Home Assistant
  switching that zone; it may have been switched off in the Govee app since), is switched on first
  and then changed, which is the order the app takes. The confirmation times are the ones
  `govee-ble-local` 1.5.0 keeps per zone; diagnostics show how many seconds ago each zone was
  confirmed. If switching the zone on fails, the change isn't sent. A plain turn on still always
  sends the zone's power. The self-test puts a device back in the same order: on first, then its
  colours, then off last.
- **Background polls give way to commands.** A background poll that comes due while a command to
  the same device is running, or within 25 s after one, now waits until then (while the command's
  connection is still open, so the poll reuses it), and a second tap or a slider step doesn't queue
  behind the poll's reads. A steady stream of commands holds a poll off for at most 2 minutes. A
  device whose polls take 10 s or more, or that is heard at -90 dBm or weaker, is polled less often:
  its interval doubles with each slow poll, up to 8 times and within the 30-minute cap, also after a
  failed poll or a change, and a fast poll at a usable signal resets it. After a restart the first
  polls spread over 5 s per active device (at least 30 s, at most 100 s) instead of always 30 s.
  A background poll's connect also gives up after 15 s, so a device that doesn't answer no longer
  holds up a command to it for up to 45 s; commands keep the library's full connect budget. A poll
  that times out after 10 s or more, such as one whose connect ran out of those 15 s, backs off as
  a slow poll (so at most every 16 minutes) rather than to the 30-minute cap, and still counts as a
  failed poll. However far apart a device's polls are, it now goes unavailable once Home
  Assistant's Bluetooth stops listing it and it has also gone 6 minutes without being heard or
  reached by a command or poll (or has failed three polls). Through a Bluetooth proxy, Home
  Assistant stops listing a device 3 to 9 minutes after its last advertisement, so it goes
  unavailable within about 9 minutes; through a local adapter, at Home Assistant's next 5-minute
  check after BlueZ drops it. A device commanded every few seconds, which doesn't advertise while
  that keeps its link open, stays available: its polls use that link. A device heard less often
  than about every 6 minutes can now show unavailable in the gaps between its advertisements
  (through a proxy, always once its gaps are longer than about 9 minutes). The diagnostics show
  the slow-poll steps and how long the last poll took.
- The RSSI, last seen, last connected and poll interval diagnostic sensors are disabled by default
  for newly added devices (existing ones keep their setting).
- Poll errors and the rejected-secret error are translated.
- Internal clean-up with no behaviour change: entity/platform ownership rules, the command
  publish pattern, the secret-rejected handling and the colour-temperature range each live in one
  place; config-entry setup closes the BLE link through an on-unload job; entities link to the
  device record instead of re-declaring it; platforms use `AddConfigEntryEntitiesCallback`; the
  config flow keeps the pending device in one shape; formatting follows `ruff format`; the
  self-test report, its steps and the `capture_session` entries are typed (`TypedDict`), and the
  self-test read-back checks name the state fields directly instead of looking them up by string.
- Segment lights and the self-test's segment check read the segment count through one helper: the
  library's runtime `Device.segment_count`. No entities change yet: segment lights are created at
  setup, before the first poll has read a device's own count, so they still follow the profile. An
  H6062, the only model that reports its own count (`aa 41` sectionNum), still gets its profile's 33
  segment lights, and the library rejects indices at or above the count it reports (open,
  `docs/library-review.md` 7.3).
- The cloud secret step matches an account device only by the BLE MAC the library reports
  (`deviceSettings.address`, else the last 6 octets of the device id). It no longer falls back to
  the raw device id, so a device whose id has fewer than 6 colon-separated octets (the library
  then reports no BLE MAC) now gets "secret not found". Govee's 8-octet ids are unaffected.
- Tests: the Govee-cloud fixtures match what the library's account parser builds (a device id
  ending in the Wi-Fi MAC, the BLE MAC from `deviceSettings.address`); a device listed without
  that address is shown not to match its Wi-Fi MAC.
- Tests: assertions that couldn't fail now discriminate, and new tests cover the presence timer,
  availability without polling, diagnostics redaction, the logger integration, flow cancellation,
  reauth/reconfigure reloads and the manifest requirement shape.
- Tests: the every-profile entity-structure check also runs over each model the library offers only
  as an opt-in (unverified) one, set up with the opt-in, taken from the library rather than a
  fixed list.
- `docs/library-review.md` lists the problems whose fix depends on the `govee-ble-local` library
  (self-test restore and read-back, the segment/zone light state model, connection-slot use and
  more), to be decided after the library refactor.
- Tests and type checks run against Home Assistant 2026.10 (`homeassistant==2026.10.0`,
  `pytest-homeassistant-custom-component==0.13.371`), and CI uses `actions/checkout` v7 and
  `actions/setup-python` v7 (Node 24).
- The config flow type-checks against probatio, which Home Assistant 2026.10 uses for flow schemas
  (it aliases `voluptuous` to probatio at runtime). At runtime the flow imports `voluptuous`,
  which Home Assistant aliases to probatio.

## [1.4.1] - 2026-10-13

Requires `govee-ble-local` 1.2.1 or newer (any 1.x) **from PyPI** and Home Assistant 2025.10+.

### Fixed
- **Integration failed to load on Home Assistant 2026.10** ("Not loaded"; dependency install
  failed): `govee-ble-local` 1.2.0 capped `cryptography` below 49 while Home Assistant 2026.10
  requires `cryptography==50.0.1`. Library 1.2.1 drops the cap
  ([govee-ble-local#8](https://github.com/Brady-Woods/govee-ble-local/issues/8)).

### Changed
- The library requirement is now a range, `govee-ble-local>=1.2.1,<2`, instead of an exact pin:
  library fixes (like the one above) reach users without waiting for an integration release, and
  only a new major library version needs one. CI tests both the oldest allowed library version
  and the newest release in the range.
- Library 1.2.1 also reads device names the way the Govee app does (devices advertising a bare
  `GV…` name were never discovered).
- Passes Home Assistant's current hassfest and HACS validation again (failing since September as
  their rules tightened): the manifest has `documentation` and `issue_tracker` links; `capture_session` targets are
  filtered by this integration's entities instead of a device filter (picking a device, entity or
  area works as before; the service call is unchanged); the "unknown scene" error no longer quotes
  its placeholder.

## [1.4.0] - 2026-10-13

Requires `govee-ble-local` 1.2.0 **from PyPI** and Home Assistant 2025.10+.

### Changed
- Bumped to `govee-ble-local` 1.2.0. Passes the typed `AdvertisementData` to `ingest_advertisement`.
  Relay-plug time sync is now best-effort in the library, so a failed follow-up no longer errors the command.

### Fixed
- **Segment lights couldn't be switched off and back on.** Turning a segment off flipped it back
  to "on" immediately (read-back brightness overrode the black-out), and turning it on again
  re-sent black. "On" now restores the last real colour; brightness-only "on" re-lights it too.
- **Reconfigure wiped a plug's secret** when submitted with the (blank) secret field untouched.
  A blank field now keeps the stored key; a "remove" checkbox clears it. Reauth rejects a blank key.
- **Self-test could leave the light off or the zones changed.** The restore stopped at the first
  failure (skipping the final power restore), never ran when a step raised, and the final
  whole-device power switched every zone back on. Every restore action now runs independently in
  a `finally`, zones are restored after whole-device power, and failures are reported
  (`restored` / `restore_errors`) and logged as a warning.
- **Plug secret recoverable from captured frames** (`wire=` was unmasked). Frames of
  secret-gated devices drop `wire=` and handshake lines (library 1.1.0 masks them too).
- Diagnostics scrub the MAC (any case/format, incl. reversed byte order), Wi-Fi MAC, serial and
  secret from every string — step errors, frames and warnings included.
- Passive advertisements no longer reset the poll timer, mark a failing poll successful, re-arm
  polling after an auth failure, or re-arm the "error fetching" log line.
- A device returning after an absence is polled immediately instead of waiting out up to 30 min
  of backoff; skipped polls no longer count as connection failures.
- Backoff jitter is applied after the cap, so long-offline devices don't poll in lockstep.
- Device-registry firmware/hardware version and serial are no longer cleared on every restart.
- `light.turn_on` with no attributes always sends power (it was a silent no-op when HA believed
  the light was on, and left a second zone off).
- Zone lights/switches show off as soon as an advertisement reports the fixture off.
- A command that fails part-way still publishes what landed; unknown effects are rejected before
  anything is sent; colour temperature is clamped to the device range; unsupported requests raise
  a validation error instead of "could not communicate".
- Unload stops polling before closing the BLE link.
- An unsupported model fails setup permanently; a malformed stored secret starts reauth.
- RSSI updates from advertisements and goes unavailable when no scanner hears the device.

### Changed
- `govee-ble-local` is installed from PyPI (`==1.1.0`) instead of a git tag.
- `capture_session` is admin-only, reports per-device errors instead of aborting, and raises a
  translated error when nothing matched. A second concurrent self-test is refused.
- Entity commands time out after 60 s instead of queueing indefinitely.
- Reading a plug's secret during setup shows a progress step; Govee credentials use
  password/email fields.
- Self-test capture no longer floods the Home Assistant log with every device's debug output.
- Diagnostic-only changes (last seen, RSSI, failure count, poll interval) only rewrite the
  diagnostic entities.
- Minimum Home Assistant version is now 2025.10.0 (`hacs.json`).

## [1.3.0] - 2026-10-12

Requires `govee-ble-local` 1.1.0. Validated live on the lab instance (11 devices).

### Fixed
- **Devices stuck unavailable.** The backoff computed `timedelta * 2**failures` uncapped and hit
  `OverflowError` after ~30 consecutive failures (1,347 "Unexpected error" tracebacks in 5 days),
  which also froze the poll interval. Backoff is now capped.
- **Availability follows reachability**, not last-poll success: a device that is advertising
  stays available (and controllable) through transient poll failures.
- Polls skip devices no scanner can see instead of spending ~80 s on a hopeless connect while
  holding a poll slot.
- Removed the slot "exhausted" pre-check and one-shot-sized semaphore: with no allocation data it
  read one adapter as zero free slots and skipped every poll forever. Replaced by a fixed
  integration-wide cap on concurrent polls.
- `capture_session` service crashed on current Home Assistant (`async_extract_config_entry_ids`
  signature); its schema now validates targets.
- The diagnostics download included the plug secret key; it is redacted (and masked in captured
  frames).
- Entities update together after a command (turning the fixture off now updates zone lights and
  switches immediately).
- Advertisements no longer write every entity's state several times a second, and no longer
  re-run the device-registry sync; "Last seen" publishes at most once a minute.
- HA brightness 1–2 no longer rounds to 0 %.
- Unload releases the BLE link after the platforms unload (and the device refuses further I/O).
- Self-tests are serialized and their captures filtered to the device under test.

### Changed
- Light commands are batched: power + brightness + colour go out as one device operation on one
  connection (H6047 whole-fixture power is a single frame).
- Reconfigure no longer requires the device to be in range.
- Test harness pinned to the production Home Assistant release; CI installs exactly the library
  the manifest declares.
- Quality scale claimed as **gold** (was platinum); see `quality_scale.yaml` for the one open
  rule (dependency-transparency: publish the library to PyPI).

### Added
- Re-authentication flow when a device rejects its secret key.
- The cloud secret lookup uses Home Assistant's shared HTTP session.
- Brand images bundled in the integration (placeholders).

## [1.2.1] - 2026-07-12

### Changed
- Bump the pinned `govee-ble-local` library requirement from `v1.0.0` to `v1.0.3`. Brings in
  three upstream fixes, all backward-compatible (no public API change; `scripts/check.sh`
  verified clean against the new pin — mypy `--strict`, 115 tests, 96.70% coverage):
  - **H6047 / H6641 segment colour read-back corrected.** Neither SKU actually answers the
    `0xAC` status burst; both are back to `readback="polled"` with a new, correctly-decoded
    direct per-group colour read (`mechanism_a_direct` — an interim fix had silently reused
    H61A8's decoder, which uses a different group size). H6641 also gains a live IC-count read
    instead of an approximation. Still not confirmed on real H6047/H6641 hardware — see
    `GAPS.md`.
  - **H60A6-style `0xAC` status reads now retry once on an empty parse** before leaving state
    stale, recovering a single dropped BLE notification mid-burst instead of costing a whole
    poll cycle. Field-tested by both the library (their changelog) and independently by us on
    `core` prior to this release: full state (brightness, all segments, both zones) recovered
    on retry in ~1.3s where it previously logged `state left stale`.

## [1.2.0] - 2026-07-12

Hardens the device self-test (`capture_session` service / "Run self-test" button) so it's a
genuine full integration test that unambiguously reports which device it ran against and fully
restores whatever it changed - two gaps found on review of the original implementation.

### Added
- The self-test report now includes the device's `address`, so results from multiple devices
  (the `capture_session` service can target several at once) are never ambiguous - previously
  two units of the same SKU would have been indistinguishable in the response.
- New coverage: segment brightness, the gradual/fade flag (H61A8), explicit zone-power-off, and
  whole-device power-off - each a distinct code path that wasn't previously exercised.
- Zone and segment colour changes are now verified against read-back state (tri-state, same as
  the existing checks), not just "did the command ACK".

### Fixed
- State restoration now covers everything the test actually changes, not just whole-fixture
  power/brightness/colour: every zone's power and colour, segment 0's colour/brightness, the
  originally active scene, and the gradual flag are all captured before the test and restored
  after - previously only the four whole-fixture fields were restored, silently leaving zones
  turned on, segments/zones recoloured, and any active scene cleared after every run.

## [1.1.0] - 2026-07-12

Slot-aware BLE connection scheduling, prompted by a live outage: two devices hit
`BleakOutOfConnectionSlotsError` after 10 connect attempts each when an ESPHome BLE proxy went
offline, leaving 12 devices contending for a shrunk connection-slot pool with zero cross-device
coordination beyond a one-time startup stagger.

### Added
- A domain-wide semaphore capping how many devices may be simultaneously mid-connect, sized from
  live Bluetooth connection-slot data (`homeassistant.components.bluetooth`), always reserving
  at least one slot for a manual connection (e.g. the Govee app) or another integration.
- The poll interval now dynamically lengthens when devices outnumber usable slots, and jitter is
  applied to every recurring interval and backoff-reset (not just the one-time initial stagger),
  so devices don't drift back into lockstep over uptime.
- `BleakOutOfConnectionSlotsError` (the whole pool is out of slots, not just this device) is now
  detected both proactively (before attempting a connect) and reactively, skipping the usual
  1-failure grace and backing off harder immediately, instead of retrying quickly into the same
  exhausted pool.
- A new **Connection source** diagnostic sensor showing which Bluetooth scanner (local adapter
  vs. a named ESPHome proxy) last saw each device - closes the blind spot that required manual
  log-digging to diagnose the outage above.
- Setup no longer blocks on a full connect+handshake for a device currently visible via BLE
  advertisement: it's marked ready immediately from passive state, and the real first poll runs
  as a staggered, slot-aware background task instead of serializing every device's handshake into
  the HA startup sequence. A device never seen advertising still requires a real connect before
  setup succeeds (`ConfigEntryNotReady` on failure), unchanged from before.

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

[Unreleased]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.4.1...v1.5.0
[1.4.1]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.2.1...v1.4.0
[1.2.1]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v0.12.1...v1.0.0
[0.12.1]: https://github.com/Brady-Woods/hass-govee-ble-local/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/Brady-Woods/hass-govee-ble-local/releases/tag/v0.12.0
