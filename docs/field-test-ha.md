# Pre-release field test through Home Assistant

The release decision (devlist-traces DECISIONS.md item 31) is a full test pass on every Govee
device in range of any of the user's systems: the Mac, the bench machine and Home Assistant's
proxies. This runbook covers the **Home Assistant route**: every `govee_ble_local` device on the
lab Home Assistant, reached through its Bluetooth proxies and the host's own adapter, the software
proxy that wedged before included. The Mac route is the library's `tools/field_test --all`. Host
bleak on the bench machine is off-limits on every route.

The harness is `tools/ha_field_test` (stdlib Python, run on the Home Assistant host). It is a dev
tool: it adds no dependency to the integration and isn't shipped with it.

In the commands below, `HA_HOST` is the SSH name of the lab Home Assistant host
(`export HA_HOST=…` first), and `HA_CONFIG` is the host directory mounted as the container's
`/config`.

## When, and where it sits in a release

Before **every** release, in **daytime**, started by you each time:

1. Build the release candidate: the library wheel and the integration branch, its gate green at
   both ends (`scripts/check.sh`, `LIB_VERSION=floor` and `newest`).
2. **Install the release candidate on the lab host.** This is a separate step, and you confirm it
   yourself (see below). The harness never deploys anything.
3. Dry run: read the plan.
4. Run the test. You start it, after 07:00 America/Los_Angeles.
5. Read the report and attach `report.md` to the release PR.
6. **Revert the lab host** to the released build (step 5 below).
7. Only then tag the release and publish the library to PyPI. Tagging and publishing stay yours.

The harness refuses to start before 07:00 or after 21:00 America/Los_Angeles. It checks the
window again before every step, and a run that reaches 21:00 restores the device it is on and
stops. `--outside-daytime-ok` overrides the window, and only you should pass it.

## Prerequisites

- `ssh "$HA_HOST"` works, and Home Assistant is up on `http://localhost:8123` there.
- A long-lived token is in `~/.ha_token` on that host, mode 600. The harness reads it there and
  refuses a file anyone else may read. It never prints, logs or writes the token, and it only sends
  it to localhost.
- Python 3.11 or later on the host (stdlib only, no pip), and `tmux` (or `screen`).
- The household knows that lights will change for a while.

## 1. Copy the harness to the lab host

From a checkout of the branch under test:

```bash
git archive HEAD tools/ha_field_test | ssh "$HA_HOST" 'rm -rf ~/govee-bench/ha_field_test_src && mkdir -p ~/govee-bench/ha_field_test_src && tar -x -C ~/govee-bench/ha_field_test_src'
```

The commands below run it from `~/govee-bench/ha_field_test_src/tools`.

## 2. Install the release candidate on the lab host (separate, user-confirmed)

> **Open question.** The library coordinator has asked whether installing a release candidate on
> the lab host for each run needs your own override. That isn't decided yet. Until you decide, do
> this step only with your explicit OK for that run. It is never part of the harness.

Use only the recreate-durable deploy. The `homeassistant` container is recreated on image
updates, and only `/config` survives that.

- `/config` is `$HA_CONFIG` on the host (mounted `:Z`).
- Back up the deployed integration:
  `sudo tar -C "$HA_CONFIG/custom_components" -czf ~/govee-backup-lab/govee_ble_local-<UTC>.tgz govee_ble_local`.
- Put the candidate's `custom_components/govee_ble_local` there. Build it with
  `git archive <rc commit> custom_components/govee_ble_local`, and stage it outside the config dir.
- Give the files the owner and SELinux label of `configuration.yaml`:
  `sudo chown -R --reference="$HA_CONFIG/configuration.yaml" …/govee_ble_local` and
  `sudo chcon -R --reference="$HA_CONFIG/configuration.yaml" …/govee_ble_local`.
- **The library release candidate:** Home Assistant installs the manifest's `requirements`
  itself when it starts. A release candidate that isn't on PyPI can only come from its wheel:
  temporarily point the lab copy of `manifest.json` at the GitHub pre-release's wheel URL
  (`govee-ble-local @ https://…/govee_ble_local-1.7.0rc1-py3-none-any.whl`), as for 1.6.0rc1.
  That edit is lab-only and never committed.
- Run `sudo podman restart homeassistant`.
- **Never** use `podman cp` into the container. It stamps the container's private SELinux
  categories, which go stale on the next recreate. **Never** run `pip install` in the container:
  that is wiped on recreate.

Verify:

- `sudo ls -Zd "$HA_CONFIG/custom_components/govee_ble_local"` shows the label of
  `configuration.yaml`.
- The Home Assistant log has no "Integration not found".
- Check the library version. This command is read-only:
  `sudo podman exec homeassistant python3 -c "import importlib.metadata as m; print(m.version('govee-ble-local'))"`.
- The dry run's header shows the integration version and its library requirement.

## 3. Dry run (the default; read-only)

```bash
ssh "$HA_HOST" 'cd ~/govee-bench/ha_field_test_src/tools && python3 -m ha_field_test'
```

The dry run reads Home Assistant and makes no service calls. It reads the config entries, the
device and entity registries, the states, each entry's diagnostics, the Bluetooth scanners and
10 s of advertisements (which scanners hear each device). It prints the inventory and the masked
plan, and saves `plan.md` and `plan.json` under `~/govee-bench/ha_field_test_runs/<time>-dry-run/`.
Check these:

- **Inventory:** every device by its label, with its area and the scanner Home Assistant prefers
  for it (the integration's Connection source sensor), both as neutral labels (`area 3`,
  `proxy A`). Two lists follow it:
  - "Not testable": devices that are disabled, ignored, not loaded or unreachable. A run never
    touches or even reads them.
  - "Heard, no entry": Govee devices Home Assistant hears that have no `govee_ble_local` entry.
    These can't be tested on this route.
- **Each device's steps:** what each step sends, what it expects, and any skipped parts and
  restore caveats.

## 4. Run

Inside tmux on the host (`ssh -t "$HA_HOST" tmux new -s fieldtest`):

```bash
cd ~/govee-bench/ha_field_test_src/tools && python3 -m ha_field_test --run --expect-version 1.7.0rc1 --expect-library 1.7.0rc1
```

- The harness refuses to start unless:
  - Home Assistant runs exactly the integration version `--expect-version` names;
  - the integration's library requirement names exactly the `--expect-library` version (the
    release candidate wheel URL's version, an `==` pin, or the `>=` floor), and you type that
    same version at the prompt after running step 2's read-only check. Home Assistant's API
    doesn't say which library is installed, so the report records the version you confirmed;
  - it runs inside tmux or screen (`--no-tmux` overrides). A dropped SSH session can't end a run
    there. Outside, SIGHUP makes the harness restore the device it is on and stop, and every line
    of output also goes to `run.log` in the run directory, so a terminal that hung up can't stop
    a restore.
- **Plugs:** the harness lists each plug it finds with its name, area and relay states (a
  per-outlet relay device, one switch per outlet, counts as a plug). It asks which ones may be
  switched: type their numbers, or press Enter for none. A plug you don't name is never touched.
  Switching a plug cuts power to whatever it feeds, then restores it. A plug's one switch drives
  all of its outlets, so an outlet that was off comes back on with the others: name only a plug
  whose outlets were all alike. This prompt is the only place the harness shows a real name or
  area; it is never saved.
- Type `START` to begin. The run needs a terminal (`ssh -t`).
- It tests one device at a time and sends one service call at a time, never two at once. Before
  the next device it waits until the last device's link is released (the library keeps a link up
  30 s after a write). So at most one link is up through any proxy, and there's no connect burst.
- **The fragile proxy:** the software Bluetooth proxy that wedged before (the scanner whose name
  contains `bt-proxy`; `--fragile-proxy` names another). A device counts as reachable through it
  if it is the device's Connection source, or if the proxy has discovered the device (the
  Bluetooth integration's own per-scanner list, read-only) or relayed one of its advertisements.
  Any call to such a device that fails over the radio, or takes 30 s, ends the run once that
  device is restored: a test step, a read-back poll, a restore call or the self-test. A refusal
  Home Assistant or the integration gives without the radio (a validation error) doesn't count.
  A burst through it wedged its adapter before. The harness never touches the proxy. Never stop
  that proxy's container, and never reset its adapter, without your OK.
- **Time:** with a read-back after every step, a device takes about 3-6 minutes, so the full lab
  takes about an hour. `--no-readback` is faster, but every step then counts as unverified.
  `--only 'H60A6 #2'` (or `--only H60A6`) tests a subset.

## 5. Revert the lab host to the released build

After the report, put the release back. Use the backup from step 2:

```bash
sudo rm -rf "$HA_CONFIG/custom_components/govee_ble_local" && sudo tar -C "$HA_CONFIG/custom_components" -xzf ~/govee-backup-lab/govee_ble_local-<UTC>.tgz
```

Or deploy the release itself once it ships, as in step 2. Either way:

- drop the lab pin in `manifest.json`, if the backup or release still carries one;
- give the files the label of `configuration.yaml` (`chown` and `chcon` as in step 2);
- **put the library back too.** Reverting the integration's files leaves the release candidate's
  library installed: Home Assistant keeps an installed version that satisfies the requirement's
  floor, a pre-release included. Either recreate the container from its stack (it is rebuilt with
  nothing installed, and Home Assistant installs the requirement from PyPI), or temporarily pin
  the released library in the lab copy of `manifest.json` (`govee-ble-local==<released>`),
  restart, check, then drop the pin and restart again;
- run `sudo podman restart homeassistant` (if you didn't recreate it);
- check the versions with a dry run (its header) and the read-only library check from step 2.

## Restore and abort

- **The journal.** Before the first change, the harness writes every planned device's start state
  to `journal.json` in the run directory (fsynced, so it survives the host losing power). It
  writes each device's record again just before that device's test.
  - The start state is:
    - the on/off state, brightness, colour mode, colour or Kelvin and scene;
    - each zone's power and colour, and each enabled segment light;
    - the library's state from the diagnostics: the colour and brightness a fixture that is off
      comes back on in, each segment's colour and brightness, and each zone's Kelvin.
  - Home Assistant shows no colour for a light that is off. So as the test's first power-on lights
    a zone or segment that was off, before any colour step, the journal also keeps the colour it
    shows then.
  - The journal says a device was put back only once the last check found nothing different (after
    the self-test, if one ran). It is **private**: unmasked and owner-only. Never attach it.
- **Every device is restored** after its test, and also on a failure, on Ctrl-C, SIGTERM or SIGHUP
  (`try`/`finally`). The restore starts before anything is printed. Colours are set while things
  are lit, and only then is anything switched off:
  1. The fixture, lit, with its colour and brightness: as found or, for one found off, the colour it
     kept while off.
  2. Each zone's colour, then each enabled segment's. A segment is blacked out only if its own
     colour was black: one that showed off because its zone or the fixture was off keeps its
     colour.
  3. Zones and outlets that were off, switched off again; plug relays as found.
  4. A fixture that was off, switched off.

  An on/off zone found on (a zone without segments) gets its power back alone. A zone the
  library knew in Kelvin (an H6047 bar) gets that Kelvin back. A call whose entity already shows
  what it should is skipped, and a device already as found, or that nothing was sent to, isn't
  touched at all. Then the harness forces a read-back and waits out the library's 15 s
  advertisement window, so an advertisement that flips the state back shows up. It then checks
  every entity against the journal, and the library's state too: kept colours, each segment,
  and zone Kelvin. If anything differs, it tries once more, and otherwise reports what still
  differs. A restore that fails outright is retried on a new connection. A device left with
  failed restore calls ends the run: no other device is changed while one isn't put back.
- **Ctrl-C during a restore** only prints a warning. Pressing it three times abandons the restore,
  and the journal then says the device wasn't put back.
- **Recover from an abandoned or crashed run** with:

  ```bash
  ssh -t "$HA_HOST" 'cd ~/govee-bench/ha_field_test_src/tools && python3 -m ha_field_test --restore-from ~/govee-bench/ha_field_test_runs/<run>/journal.json'
  ```

  - It considers only devices whose test began and that the journal doesn't mark as put back
    (`--all`: every tested device). It lists them and asks you to type `RESTORE`.
  - It asks separately before setting each plug relay or outlet.
  - It leaves alone a device that is already as found.
  - It refuses a device whose config entry is gone, or whose journal record names entities that
    aren't that device's own now.
  - One device's failure doesn't stop the others.
  - It needs a terminal, and keeps to the daytime window (`--outside-daytime-ok` overrides).
- **What Home Assistant can't put back** shows as "Restore caveat" in the plan, and the check
  reports it:
  - the colours of disabled segment lights, when the segments had different colours at the start;
  - a scene that was set on a fixture that was off;
  - a multi-outlet plug's outlets that were off.

## What each device gets

Every step comes from the entity's own capabilities in Home Assistant: its supported colour modes,
Kelvin range, effect list and supported features. Library capabilities come from the diagnostics.
Nothing comes from a per-model table.

| Part | Steps |
|---|---|
| Power | Off, then on. A fixture found off gets on, off, on. Every later step finds it lit. |
| Brightness | 25 % and 100 %. An on/off light gets brightness that Home Assistant drops (expected). |
| RGB | Red, plus the white tint pick. |
| White tint pick | The Govee app's 4000 K tint colour (255, 213, 161), picked as RGB. Where 4000 K is in the range, the library sends it as 4000 K and the light reads back colour temperature 4000 K. Elsewhere it stays RGB. |
| Colour temperature | The minimum, middle and maximum of the light's range. Then a Kelvin above the range, which the integration clamps to the maximum (expected). |
| Kelvin without COLOR_TEMP | Home Assistant converts it to RGB, recorded with the RGB it should become (expected). |
| RGB without RGB | Converted to a Kelvin (colour-temperature-only light) or dropped (on/off light), as expected. |
| Scene | The first scene offered, then `effect: off` (back to a solid colour). Then a scene the device doesn't have, which should be refused with `unknown_effect` and change nothing. |
| Zones | After the fixture: each zone light gets off, on, 50 %, green and the middle Kelvin (or Home Assistant's RGB for it). An on/off zone (one without segments, an H60A6 at pact 1,1) gets off, on, and the brightness and colour Home Assistant drops (expected). |
| Segments | Enabled segment lights only: magenta, 50 %, the middle Kelvin, off (blacked out), on. They are disabled by default. A library that can't set a model's segment Kelvin refuses it with `not_supported`, an accepted answer. |
| Plugs and outlets | Each relay off and on (or on and off), ending as found. Only for the plugs you name. |

`--self-test` also presses each device's **Run self-test** button after its restore, if the
window is still open, records the outcome from the diagnostics, then puts the device back and
checks it again, whatever the self-test did. A self-test call that fails or times out may still
be running in Home Assistant, so the run starts no other device after it.

## Verdicts and settling

- A service call returns once the integration's command has finished: its frames acked and its
  state written. So the state is read at once, with no fixed sleep.
- A **read-back** (`homeassistant.update_entity`) then polls the device. Home Assistant's refresh
  debouncer ignores a second request within 10 s, so the harness spaces read-backs past it, and
  each one blocks through a real poll. The diagnostics then show whether the poll succeeded.
- A device the library tracks without read-back (optimistic, such as the plug family) has each
  on/off step confirmed from its advertisement: one Home Assistant heard after the library's 15 s
  suppression window. Without one, the step is unverified. A device the library hasn't read yet
  looks optimistic too, so it is polled once first.
- Each step is one of:
  - **pass**;
  - **unverified:** accepted and shown, but neither read back nor confirmed from an
    advertisement, so possibly only the integration's own echo;
  - **fail:** the state differs (what it showed, and what was expected);
  - **error:** the call failed where it shouldn't have.

  A conversion or refusal that happened as expected passes.
- Tolerances:
  - brightness ±3 of 255 (±2 % for a segment);
  - RGB ±5 per channel;
  - Kelvin ±100.
- Warnings the integration captured during a device's test (its diagnostics' `recent_warnings`)
  are listed under the device.

## Reports

Each run directory holds `report.md` and `report.json`, both **masked**, and rewritten after
every device and whatever ends the run, plus `run.log`.

Nothing in them may identify your home, so everything is named by a neutral label, assigned in
a stable order:

- a device by its model and a number (`H60A6 #2`), never its address, entry title or names;
- a scanner by its kind and a letter (`local adapter`, `proxy A`, `proxy B`), never its hostname,
  ESPHome name or address;
- an area by a number (`area 3`), never its name;
- an entity by its role (`zone main (light)`, `segment 4`), never its entity ID, which Home
  Assistant made from a name.

As defence in depth, every saved or printed string is also masked:

- each of those identifiers, and each config entry and entity ID, becomes its label wherever it
  appears (a hex identifier in every spelling, as the integration's diagnostics match it; a name
  shorter than 6 characters as a whole word only);
- any other MAC becomes `<mac>`, with no byte left, not even the vendor prefix;
- any other Govee name loses the MAC digits it ends in (`GVH60A6xxxx`);
- any hex run of 8 or more digits becomes `<hex>`.

Errors are reported by their key and the underlying error, never by a message that names the
device. Home Assistant errors never carry a request path, and the harness refuses an HTTP
redirect, so the token can't follow one away from localhost.

Check a report before attaching it anyway: the masking can only replace what it was told about.

Each device's section lists every step with its verdict, its read-back, how long it took and the
scanner it went through, followed by the restore outcome. Attach `report.md` to the release PR.

## Options

| Option | Use |
|---|---|
| (none), `--dry-run` | Read and print the plan. No service calls. |
| `--run --expect-version X --expect-library Y` | Run the plan (asks about plugs, the installed library, then for `START`). |
| `--restore-from JOURNAL [--all]` | Put a run's devices back (asks first). |
| `--only LABEL\|SKU` | Test a subset (repeatable). |
| `--no-readback` | Skip the read-back poll after each step (every step is then unverified). |
| `--self-test` | Also run each device's self-test after its restore. |
| `--outside-daytime-ok` | Your override of the 07:00-21:00 window. |
| `--no-tmux` | Allow `--run` outside tmux or screen. |
| `--fragile-proxy NAME` | The scanner whose trouble stops the run (default `bt-proxy`). |
| `--scan-seconds N` | How long to collect advertisements (default 10). |
| `--out DIR` | Where run directories go (default `~/govee-bench/ha_field_test_runs`). |

## Not covered here

- Govee devices with no `govee_ble_local` entry in Home Assistant. The inventory lists them, but
  this route can't drive them.
- Devices only the Mac reaches: the library's `tools/field_test --all` covers those.
- The H5122 button: it has no verified local format, so it isn't implemented.
