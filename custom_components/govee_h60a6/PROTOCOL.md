# Govee H60A6 BLE Protocol Reference

Reverse-engineered entirely from scratch for this specific SKU (15" RGBICWW
Ceiling Light Pro) via live BLE captures (Android `btsnoop_hci.log` pulled
through `adb bugreport`), cross-referenced against a handful of independent
community projects covering *other* Govee models (none of which tested the
H60A6 directly — see "Prior art" at the end). Everything here is confirmed
against real device behavior unless explicitly marked as a hypothesis.

Reference device used throughout: BLE address `5C:E7:53:F4:74:57`, SKU
`H60A6`, hardware version `1.04.03`.

## 1. GATT layer

| Item | Value |
|---|---|
| Service UUID | `00010203-0405-0607-0809-0a0b0c0d1910` |
| Write characteristic | `00010203-0405-0607-0809-0a0b0c0d2b11` (write-without-response) |
| Notify characteristic | `00010203-0405-0607-0809-0a0b0c0d2b10` |

Confirmed by live GATT service discovery against the real device (not
assumed from other models — other Govee generations use different handle
layouts and in some cases entirely different service UUID schemes).

The device does **not** expose the standard BLE Device Information Service
(`0x180A`). Firmware version, hardware version, serial number etc. are not
available via any standard characteristic — everything is inside this
vendor-specific channel.

## 2. Packet framing

Every packet, in both directions, is exactly **20 bytes**:

```
[opcode/command byte(s)] [payload] [XOR checksum]
```

- Byte 19 (last byte) = XOR of bytes 0-18.
- Before encryption, packets are built as a 19-byte body (opcode + payload,
  zero-padded) + 1 checksum byte.

## 3. Encryption

All traffic on the write/notify characteristics (after the initial
handshake) is encrypted:

- **Bytes 0-15**: AES-128-ECB, no padding (block-for-block).
- **Bytes 16-19**: RC4 stream cipher, same key.

This scheme is not unique to this device — it matches the encryption used
by [wcbonner/GoveeBTTempLogger](https://github.com/wcbonner/GoveeBTTempLogger)
for a completely different Govee product line (temperature/humidity
sensors), and turned out to apply directly here. It is likely a
company-wide standard for Govee's newer BLE-secured devices, not
device-specific.

### 3.1 Handshake / session key derivation

1. App writes `[0xE7, 0x01]` + 14 zero bytes, checksummed, encrypted with
   the **pre-shared key** `"MakingLifeSmarte"` (ASCII, 16 bytes) — AES-ECB
   on bytes 0-15, RC4 on bytes 16-19, same as any other packet.
2. Device notifies back `[0xE7, 0x01, <16-byte session key>, ...]`,
   encrypted the same way **with the PSK**. Decrypt with the PSK; bytes
   `[2:18]` of the decrypted plaintext are the session key for the rest of
   the connection.
3. App writes a second handshake packet `[0xE7, 0x02]` + zeros (also
   PSK-encrypted). The device's ack to this doesn't need to be decoded for
   anything — connection is now ready for normal commands.
4. All subsequent packets use the same AES-ECB+RC4 scheme, but keyed with
   the session key instead of the PSK.

The session key is per-connection (re-derived on every reconnect), not
persistent.

## 4. Confirmed command opcodes

All commands below are sent as `write-without-response` and generally get
an ack notification back (encrypted, same key) — though **the payload of
that ack is not meaningful for most commands** (in some captures it's an
echo, in others a generic status ping); only its *presence* is used to
confirm the command was received. The one case where notification content
matters is the scene-upload completion ack (§6) and status queries (§5).

| Prefix (plaintext, pre-checksum) | Meaning |
|---|---|
| `33 30 <zone> <state>` | Zone on/off. `zone`: `0`=lower panel, `1`=upper ring. `state`: `0`/`1` = off/on. Sending this command is solid (physically verified); *reading the resulting state back* via status query is not — see §5.2. |
| `33 04 <pct>` | Brightness, 0-100 decimal in one byte. |
| `33 05 04 <id_lo> <id_hi>` | Activate a scene by its 16-bit little-endian scene code (see §7). |
| `33 05 15 01 <r> <g> <b> 00 00 00 00 00 ff 1f` | Set solid RGB color. |
| `33 05 15 01 ff ff ff <k_hi> <k_lo> <ar> <ag> <ab> ff 1f` | Set color temperature. `k_hi`/`k_lo` = big-endian Kelvin value. `ar/ag/ab` = an approximate RGB tint for that color temperature (see §4.1) — appears to be cosmetic, not authoritative. |
| `aa 00 ...` (all zero payload) | Heartbeat/keepalive. Sent by the app every ~2s while connected; device echoes it back identically. Not required for command correctness, just observed app behavior. |
| `ac 03 02 41 30` | Status query trigger (see §5). |
| `a3 <seq> <17 bytes>` | Scene/effect data chunk (see §6). |
| `ab 01 <field_id> ...` | Device metadata field query (see §8). |

Confirmed **not** available over BLE (cloud-only, per live testing with
phone in airplane mode — see §9): Power-off Memory setting, device display
name. Confirmed available but not yet mapped: calibration (rotation
adjustment) - a real, BLE-exposed feature the app uses, but the exact
command was not captured/decoded.

**`33 05 15 01` is also documented for a different Govee product
generation (H6072) as a "switch to color mode" trigger** — sent as a
priming step *before* a separate color-set command, not bundled together.
On the H60A6, the mode-switch and the color/temp value are combined into
one packet. This corroborates that the mode-switch semantics are real
(see §5.1 for why this matters for status parsing), even though the exact
packet shape differs between device generations.

### 4.1 Kelvin → RGB approximation

The `ar/ag/ab` tint bytes in the color-temp command were reverse-engineered
by capturing "max warmth" and "max cool" from the real app:

| Kelvin | Captured RGB tint |
|---|---|
| 2700 (warmest) | `(255, 174, 84)` |
| 6500 (coolest) | `(255, 249, 251)` |

These match the standard black-body radiation Kelvin→RGB approximation
(Tanner Helland's algorithm) within a few units per channel:

| Kelvin | Formula output | Real captured |
|---|---|---|
| 2700 | `(255, 167, 87)` | `(255, 174, 84)` |
| 6500 | `(255, 254, 250)` | `(255, 249, 251)` |

Close enough that the formula is used directly for arbitrary Kelvin values
rather than needing more reference points. **2700K–6500K is this device's
actual supported range** (confirmed as the app's slider min/max), not an
arbitrary choice.

### 4.2 Per-segment color and brightness control (newly discovered)

A fresh BLE capture (`btsnoop_hci.log`, phone connected via Bazzite,
2026-07-02) of the app's per-segment controls revealed a **16-bit
bitmask sub-family of `33 05 15`**, distinct from the solid-color/color-temp
commands in §4 (which always use a fixed `ff 1f` trailer and no bitmask).
This is a real capability this device has that neither this project nor
any prior-art source (§11) had previously found or used.

**Confirmed structure** (checksum verified byte-for-byte against this
project's own `_checksum` implementation - identical algorithm, no
surprises):

| Prefix | Meaning |
|---|---|
| `33 05 15 01 <r> <g> <b> 00 00 00 00 00 <mask_lo> <mask_hi> 00 00 00 00 00` | Set RGB color on the segment(s) selected by the 16-bit little-endian bitmask. |
| `33 05 15 02 <pct> <mask_lo> <mask_hi> 00 00 00 00 00 00 00 00 00 00` | Set brightness (0-100 decimal) on the segment(s) selected by the same bitmask scheme. |

Both ack with a generic `33 05 00...` (presence-only, not content-meaningful,
consistent with §4's general note on ack payloads).

**Bitmask, confirmed via real capture**: the app was used to tap through
individual segments one at a time, producing exactly one bit set per
command. Observed bits, in the order tapped:

```
color family (33 05 15 01), descending through two groups of 6:
  0x0020 (bit 5)  0x0010 (bit 4)  0x0008 (bit 3)  0x0004 (bit 2)  0x0002 (bit 1)  0x0001 (bit 0)
  0x0800 (bit 11) 0x0400 (bit 10) 0x0200 (bit 9)  0x0100 (bit 8)  0x0080 (bit 7)  0x0040 (bit 6)
```

**This strongly suggests 12 individually-addressable segments** (bits
0-11), tapped in two groups of 6 in the app's UI - plausibly one group of
6 per physical zone (upper ring / lower panel), though which bits map to
which physical zone has not yet been confirmed by observation (would need
a live test watching which physical LEDs actually light up for a given
bit, not just capturing the command). **Bits 12-15 were never observed
set in this capture** - unknown whether they're unused, or whether the
app's UI simply didn't expose a 13th+ segment to tap.

The brightness family (`33 05 15 02`) was captured mid-slider-drag, so
most samples show a *decreasing* percentage (100 → 100 → 90 → 81 → 70 →
60 → 50 → 41 → 29 → 19 → 10 → 5 → 1) against a shifting bitmask - consistent
with "set brightness `<pct>` on segment(s) `<mask>`" fired continuously
as a slider is dragged across different segment icons. One sample showed
two adjacent bits set simultaneously (`0x0030` = bits 4 and 5) - plausibly
a brief multi-segment selection mid-drag, not evidence of a different
field meaning.

**Not yet done, and needed before trusting this further**:
- Live testing to confirm which bit actually corresponds to which
  physical segment/LED position (capture-only evidence establishes the
  *command format*, not the *physical mapping*).
- Testing bits 12-15 and combinations of multiple bits set at once
  deliberately (not just as a drag artifact) to see if genuine
  multi-segment-at-once addressing works as expected.
- Checking whether `ac` status queries reveal any per-segment state after
  using this command family (a quick survey of this same capture found
  the status query traffic unchanged - still just the same simple
  heartbeat/device-info chunks documented in §5 - so per-segment state is
  likely still not exposed via status readback, consistent with
  everything else found about this device's status query).

## 5. Status query (`ac` opcode)

Request: `ac 03 02 41 30` (checksummed, encrypted with session key).

Response: a **multi-chunk** reply, each chunk a normal 20-byte encrypted
packet with opcode `0xAC`, second byte = chunk sequence number. Sequence
numbers observed: `0x00, 0x01, 0x02, 0x03, 0x04`, then a final chunk always
tagged `0xFF` regardless of how many numbered chunks preceded it (same
chunking convention as scene data uploads, §6).

**Do not assume exactly 6 chunks always arrive, or that any given absolute
byte offset is stable — see §5.1.** The device has occasionally been
observed sending a chunk type outside this set; naively counting "any 6
chunks received" instead of waiting for the *specific* keys needed caused
real, hard-to-diagnose bugs (one of the chunks we needed would go missing
while an unrecognized one took its place in the count).

### 5.1 Mode-dependent layout (important, non-obvious)

**The status response layout changes depending on whether the device is
currently in "scene mode" vs. "RGB/color-temp mode".** This was the single
most confusing bug encountered in this whole investigation, because it
looks exactly like data corruption (wrong MAC address, garbled hardware
version) rather than a structural protocol difference.

Confirmed via side-by-side real capture comparison:

- In **scene mode**, chunk `0x00` is present and carries brightness (byte
  10) and current scene ID (bytes 14-15).
- In **RGB/color-temp mode**, chunk `0x00` is **omitted entirely**, and
  every subsequent chunk's content shifts by **exactly one byte** relative
  to scene mode.

Example (same device, same fields, two different modes):

```
scene mode  chunk 0x01: 07 06 57 74 f4 53 e7 5c 07 11 10 56 74 f4 53 e7 5c
rgb mode    chunk 0x01: 07 07 06 57 74 f4 53 e7 5c 07 11 10 56 74 f4 53 e7
```

Fixed absolute byte offsets are **not safe**. The robust approach used in
the implementation: search for the device's own known BLE MAC address
(reversed byte order, as it appears on the wire — the client always knows
this independently, since it's literally the connection address) within
the reassembled `[chunk01, chunk02, chunk03, chunk04, chunkFF]` byte
stream, then use **offsets relative to that anchor point**, which stay
stable across both modes:

| Field | Offset relative to BLE MAC anchor | Length |
|---|---|---|
| BLE MAC (reversed) | `0` | 6 bytes |
| WiFi MAC (reversed) | `+9` | 6 bytes |
| Hardware version | `+20` | 3 bytes (`major.minor.patch`, plain decimal, e.g. `01 04 03` → `1.04.03`) |

For the zone on/off state (in chunk `0xFF`, bytes 13-14 in the original
scene-mode capture), the implementation currently applies a +1 shift when
chunk `0x00` is absent, on the assumption that presence of chunk `0x00`
is a reliable "which layout am I parsing" signal. **A live controlled test
(§5.2) disproved this assumption** — chunk `0x00` can be present even
while the device is in RGB/color mode, and the two-independent-flag model
for bytes 13/14 does not hold in that state. The MAC/hardware-version
anchor-based fix (above) is solid; **the zone-state parsing is not** — see
§5.2 and §10.

Brightness and current scene ID appear to be tied to chunk `0x00`'s
presence specifically, not to "scene mode" as a concept — treat these two
facts as independent until proven otherwise (see §5.2).

### 5.2 Zone-state byte encoding — live-tested, NOT fully solved

A live round-trip test (real device, controlled command, then raw status
readback — see `test_live_device.py`) caught a real gap that the static
unit tests (built only from previously-captured fixtures) could not: every
prior real capture happened to have both zones in the *same* state (both
on, or both off), so a simple "byte 13 = lower flag, byte 14 = upper flag,
0/1 each" model was never actually tested against a case where the two
zones differ. It doesn't hold:

Commanded state: zone upper = **off**, zone lower = **on** (verified by
sending `33 30 1 0` then `33 30 0 1` and physically observing the light
earlier in this investigation — the *commands* are solid; what's in
question is only the *status readback*). Immediately after, a raw status
query returned (chunk `0x00` present, contradicting the presence-as-mode-
signal assumption above):

```
chunk 0xFF: 00 00 00 80 00 00 00 80 41 02 02 01 30 02 01 00 00
             0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16
```

- Byte 13 = `0x02`. Byte 14 = `0x01`.
- The old model (`bool(byte13)` = lower, `bool(byte14)` = upper) gives
  lower=True, upper=True — **wrong** (upper should be off).
- Interpreting byte 14 alone as a 2-bit packed field (bit0=lower,
  bit1=upper) gives `0x01` = lower on, upper off — **matches** the
  commanded state.
- Byte 13 (`0x02`) does **not** fit the same 2-bit scheme consistently
  with byte 14 (it would decode to the opposite state) — it may be an
  unrelated field (a "previous state", "requested vs. actual", or
  something else entirely), or the packing scheme itself may differ from
  this guess.

This is genuinely unresolved. Do not trust the current zone-state parsing
in `client.py` under color/RGB modes — it was verified visually-correct
for the original scene-mode-only test sequence (four direct commands, each
physically observed - see the zone opcode confirmation in §4), but the
*status query's report of that state* has an unconfirmed encoding once
color commands are involved. Next step: multiple controlled captures
varying zone state combinations (upper-only, lower-only, both, neither)
while deliberately toggling between chunk-0x00-present and -absent
conditions, to isolate what byte 13 actually represents.

### 5.2.1 Real-world consequence: broader than "scene mode," and now mitigated in the UI

After §6.2.1/§6.4's fixes made scene activation actually succeed
reliably (instead of silently no-oping most of the time under the old
bare-activation default), a user report of "unable to turn the light on
and off" traced back to exactly this unresolved bug, not a new
regression. First pass at the sequence, confirmed from the live HA log: a
scene was activated on a device, leaving it in scene mode (non-default
`scene_id`); zone toggles were sent and acked normally (matching the
known-correct `33 30 <zone> <state>` format), but the following status
poll still reported the *previous* zone state. A quick A/B check (setting
a solid RGB color to exit scene mode via the Cloud API, from §6.5)
appeared to confirm scene mode as the trigger - status reporting looked
correct again immediately afterward.

**That first diagnosis was too narrow.** Further live testing (isolated
to one device with no HA/adapter contention, arbitrary delays up to 2s
before the status query) reproduced the same stale/wrong zone readback
with the device in a completely normal, non-scene state
(`scene_id=(1, 0)`) - so this is not a "scene mode" trigger specifically.
A more targeted test made this clearer still: commanding the *lower* zone
off (while upper was already off) produced a status reading claiming
*upper* had changed instead, while lower still read as on - inconsistent
with a simple "stale value" explanation and inconsistent with a simple
"upper/lower are swapped" explanation too (a follow-up command targeting
upper produced no reported change at all). The honest conclusion: this is
the same core zone-state decoding mystery from §5.2, just confirmed to be
triggerable any time the two zones differ - which is essentially any
ordinary on/off toggle, not a special case - making it far more
consequential than originally scoped.

**Mitigation applied, then reverted - the real lesson is below.**
`light.py`/`switch.py` briefly tracked on/off **optimistically** from the
last command issued (`_optimistic_is_on`), the same pattern used
elsewhere in this project for RGB/color-temp (which have no BLE readback
at all). This made the HA UI reflect what was commanded rather than the
unreliable decode - but it had a real cost: if zone state changed from
*outside* HA (the physical Govee app, a remote), HA would never reflect
it, since the optimistic value always won once set. This wasn't
theoretical - a user reported HA no longer staying in sync with app-side
changes almost immediately after the mitigation shipped.

**More importantly, it turned out to be solving the wrong problem for the
case that prompted it.** Continued investigation of a persistent "still
can't turn the light on/off" report - after the optimistic mitigation was
already live - found the device's power state was stuck at the hardware
level, confirmed by commanding power off through Govee's completely
separate, official Cloud API and observing the same non-response (a
`powerSwitch` command reported "success" but the device's own subsequent
state query still showed it on). Brightness/color continued to work
correctly on the same device throughout, over both BLE and cloud. That
pattern - LED driver responsive, power/relay logic unresponsive, across
two independent control paths - is a hardware/firmware wedge, not a
protocol decoding bug. A physical power cycle resolved it.

**Given that, the optimistic-tracking mitigation was reverted.** It cost
real functionality (external-change sync) to paper over a symptom that,
in the case that triggered it, wasn't actually caused by the decoding bug
this section documents at all. The zone-state decoding bug itself is
real and still unresolved (see the byte-level evidence above), but
"on/off doesn't seem to respond" has at least two distinct possible
causes now - the decode issue, and a device getting hardware-wedged - and
conflating them led to a fix that made a real, unrelated problem (sync
with external changes) worse without reliably solving the one it targeted.
If a similar report recurs, check for a stuck device first (cross-check
via the Cloud API control path, independent of this project's BLE code)
before assuming it's this decoding bug.

**Still fully unresolved**: the actual byte-level zone-state encoding in
`_parse_status()`, needed for anything that depends on genuinely reading
the device's real-world zone state (e.g. detecting an external change
made via the Govee app).

## 6. Scene / effect data upload (`a3` opcode)

### 6.1 Framing

Confirmed via 3+ independent real captures (Graffiti, Christmas, Cornfield)
that this is the *exact* structure:

1. Take the scene's raw effect data (see §7 for where this comes from).
2. **Set bit `0x08` on byte 0** of that raw data. This is critical and
   easy to miss — see §6.3.
3. Prepend a 2-byte header: `[0x01, chunk_count]`, where `chunk_count =
   ceil((2 + len(data)) / 17)`.
4. Split the resulting `[header + data]` byte stream into consecutive
   17-byte pieces.
5. Send each piece as a 20-byte packet: `[0xA3, seq, <17 bytes, zero
   padded>, checksum]`. `seq` is `0x00, 0x01, 0x02, ...` for every piece
   *except the last*, which is always tagged `0xFF` regardless of how many
   numbered pieces came before it.
6. After the scene ID activation (`33 05 04 <id>`), the same chunk-then-
   activate flow was also observed for **DIY/custom effects** ("Finger
   Sketch" in the app) using subcommand `33 05 0a <id>` instead of `33 05
   04 <id>` — same upload mechanism, different activation opcode,
   presumably distinguishing "built-in library scene" from "user-created
   effect".

### 6.2 Timing (critical, non-obvious)

**All chunks are sent back-to-back with zero delay between them, and the
device sends exactly one completion acknowledgment after the full burst —
not one ack per chunk.** Confirmed identical across every real capture
examined: total burst time under 20ms regardless of chunk count, single
ack arriving ~90-100ms after the last chunk.

The original implementation mistake (worth documenting so it isn't
repeated): waiting for a per-chunk ack with a multi-second timeout, and
retrying failed chunks individually. This doesn't match the real protocol
at all — spacing chunks out that much appears to exceed the device's
internal reassembly window, silently corrupting the upload (the individual
writes still "succeed" and the scene ID still gets accepted and reported
back correctly by a subsequent status query, but the light doesn't
actually render the intended pattern). The fix was a straight burst-write
of all chunks under one lock, then a single bounded wait for one ack.

For **large** scenes (many chunks), the device appears to need a brief
window afterward to finish internal processing before it can reliably
answer anything else (e.g. an immediate status query can get zero
response). A settle delay scaled to chunk count (roughly 0.2s + 0.1s per
chunk, capped at 2s) was added empirically; the exact minimum wasn't
precisely characterized.

#### 6.2.1 The ack itself is not a reliable success signal (found via live testing)

A second, separate bug beyond the timing issue above: the implementation
originally **raised and aborted the whole scene activation** if the
post-upload ack didn't arrive within 3 seconds. A dedicated live
reliability test (`test_scene_switching.py`, real device, 6 real named
scenes fetched from the live library, repeated switching in both normal
and rapid cadence - 36 total switches) measured only a **36% success
rate (13/36)** under this design, with `No ack after uploading scene data`
as the dominant failure.

Two things were wrong, both found by comparing against
[Beshelmek/govee_ble_lights](https://github.com/Beshelmek/govee_ble_lights)
(§11), an independently-developed, working Govee BLE light integration
that **never waits for or reads any command ack at all** - it fires
writes and moves on:

1. `send_command()` (used for bare scene activation, zone, brightness, and
   color commands) never drained the notification queue before writing,
   unlike every other write path (`_handshake`, `_query_status_chunks`,
   the scene-upload burst itself). A late ack from a *previous* operation
   sitting in the queue would get consumed as the ack for an unrelated
   *current* command, and vice versa - a plausible direct cause of the one
   observed case of a genuinely wrong reading after a switch
   (`scene_id=(216, 74) expected (136, 74)`).
2. `set_scene_full()` treated a missing upload ack as proof the upload
   failed and aborted before ever attempting activation. The write itself
   had already gone out regardless of whether the ack arrived in time.

Fix: added the missing queue-drain to `send_command()`, and changed the
upload-ack timeout in `set_scene_full()` from a hard failure to a debug
log (matching the working reference implementation's design - proceed to
activation regardless). Re-running the identical live test afterward:
**30/36 (83%)**, with 5 of the 6 tested scenes (Forest, Desert, Sunset,
Aurora, Rainbow) now **100% reliable** across all 36 of their individual
attempts. The remaining failures are isolated to one scene (Ocean) - see
§6.3's updated large-scene note, not a general reliability problem
anymore.

One caveat worth recording honestly: the same test's own ack-vs-success
correlation check found ack-received attempts succeeded 30/31 while
ack-NOT-received attempts succeeded 0/5 - i.e. in this run the ack *was*
still a meaningful (if imperfect and non-blocking-worthy) signal, not
pure noise. All 5 no-ack cases happened to be the Ocean scene specifically
(see §6.3), so this may just reflect Ocean's independent size-related
problem rather than the ack being generally informative. Treating a
missing ack as "proceed but log for visibility" rather than "hard fail"
was still the right call - a 3-in-36 blind spot beats a 23-in-36 false
failure rate - but this is not fully settled and the ack shouldn't be
dismissed as meaningless without more data across more scenes.

### 6.3 The "unconfirmed template" flag bit

This was the hardest bug in the whole investigation to pin down. Data
fetched from Govee's public scene-library API (§7) has bit `0x08` of byte
0 **unset**. Uploading it unmodified:

- Gets acked normally (upload ack, activation ack both succeed).
- Updates the device's reported current scene ID correctly (a subsequent
  status query shows the right scene code).
- **Does not actually render** — the light goes dark/off instead of
  showing the scene.

Setting that one bit (`data[0] |= 0x08`) before uploading, with *everything
else* identical, fixes it completely. Confirmed via a controlled A/B test
sending the literal same bytes except for that one bit. Working
hypothesis: this bit distinguishes "confirmed/customized" data from a raw
default template, and the device silently no-ops if it looks like
untouched template data. Real captures show the actual app always sends it
set.

**This single-bit fix is confirmed sufficient for small/medium scenes only
(up to 6 chunks in verified real-vs-implementation comparisons).** Larger,
apparently multi-segment scenes (Desert, Volcano, Winter, Ocean — 12-22
chunks) were originally believed to still fail to render correctly even
with the flag bit set and the correct chunk framing.

**Update, revised after the ack/queue-drain fix in §6.2.1**: this earlier
belief was largely a misdiagnosis. Live testing (`test_scene_switching.py`)
after that fix found Desert (12 chunks), Aurora (12 chunks), and Sunset
(13 chunks) all **100% reliable** (6/6 real-device switches each,
normal and rapid cadence) - i.e. these were failing before purely because
of the client-side ack-handling bug, not a real protocol limitation for
scenes in that size range. This retroactively explains the original
user-observed symptom that kicked off this whole investigation
("switched to desert and it failed again and turned off the light") -
it was the ack/queue bug, not Desert's data specifically.

Only **Ocean (20 chunks, 336 bytes - the single largest scene tested)**
appeared to still fail via the automated status-query test: 0/6 across
the same test, every failure either a missing status response or (twice)
a wrong scene ID reported back.

**Further revised, after human-observed (not just status-query) testing
in real HA use - see §6.4.** The status-query method turned out to be an
unreliable proxy for actual rendering (§6.4), so the "only Ocean fails"
conclusion above was itself incomplete. Real visual testing found several
more scenes genuinely fail (Aurora, Desert, Dandelion, Fall, Green Wheat
Field, Winter, plus Ocean), and identified two *distinct* root causes
hiding under the single "large scene" label:

#### 6.3.1 Two distinct scene-upload failure modes (found via real observation)

**Cause 1: literal `0xFF` placeholder bytes in the effect header.**
Comparing the raw `scenceParam` bytes of confirmed-working vs
confirmed-failing scenes found a clean, non-coincidental split: scenes
whose header bytes 2-5 (right after the `50 20` type prefix) contain a
literal `0xFF` fail to render, **regardless of size** - Green Wheat Field
is only 141 bytes (9 chunks) and still fails, smaller than several
scenes that work fine. Checked against the full 84-scene library: 18 of
60 scenes in this data family (`byte[1] == 0x20`) have this signature,
including every single one of Aurora/Dandelion/Desert/Fall/Green Wheat
Field/**Volcano** (Volcano was flagged as broken all the way back at the
start of this investigation - independent corroboration the pattern is
real, not coincidental). Working hypothesis, unconfirmed: these `0xFF`
bytes are unresolved template placeholders (a different field from the
already-known `0x08` flag bit in byte 0) that the real app substitutes
with concrete values before upload, and the device silently no-ops when
it sees the raw placeholder - the same general shape of bug as §6.3's
main finding, just a different field. Not yet attempted: replacing these
bytes with a resolved value and re-testing live.

**Cause 2: genuinely oversized scenes.** Ocean (336 bytes/20 chunks) and
Winter (368 bytes/22 chunks) both have "clean" headers (no `0xFF`,
matching the pattern of working scenes like Spring/Sunset) but are the
two largest scenes tested, and both fail - Ocean additionally causes an
outright BLE disconnect, a more severe symptom than the others. Sunset
(211 bytes/13 chunks) and Spring (254 bytes/16 chunks) share the same
clean header pattern and work fine, so the real size threshold is
somewhere between ~254 and ~336 bytes - still not pinned down, and this
remains a **separate, unresolved** issue from Cause 1. See §10.

### 6.4 Practical note: bare activation vs. full upload

`33 05 04 <id>` **alone**, with no preceding upload, works correctly for
any scene the device already has cached from prior use (via the real app
*or* via a previous correct upload). Real capture evidence: of 69
sequential scene selections in one real session, only 11 (16%) triggered a
fresh upload — the other 58 fired bare and rendered correctly.

**Revised, no longer the default in `light.py`.** The reasoning above led
to bare activation being made the HA entity's default, on the theory that
"this device already has effectively every scene cached." That assumption
was wrong in a way the earlier automated testing (§6.2.1) didn't catch:
that testing only verified success via the status query's `scene_id`
field, never by actually watching the light. Real-world use surfaced
scene switching "still failing" in HA despite §6.2.1's fix - because that
fix only touched `set_scene_full()`, and `light.py` never called it.
Separately, live-observed testing found only ~3 of 6 scenes cycled
through actually changed the light visually as expected (a pattern
initially reported as unidentified/"fireflies"-looking was later
confirmed to just be Spring, correctly rendering - not a mystery 7th
scene). Follow-up with the real HA UI and more scenes confirmed the gap
is real and larger than 3/6: see §6.3.1 for the two actual root causes
found. This was direct evidence that a `scene_id` match in the status
response does **not** reliably mean the light rendered that scene, the
same class of gap as the flag-bit bug in §6.3.

`light.py`'s `_activate_scene()` now defaults to `set_scene_full()`
whenever real `scenceParam` data is available (i.e. the live scene library
fetch succeeded), falling back to bare activation only for the static
`SCENES` table (scene code only, no effect data, used when the live
library fetch fails). This is **not yet re-validated with actual visual
confirmation** - only via the same status-query method now known to be an
unreliable proxy for true rendering. Next needed step: a human-observed
test of scene switching through the real HA UI, not just an automated
status-query check.

### 6.5 Decisive confirmation via the official Cloud API: the bug is ours, not the data's

With a real Govee Developer API key, the two scenes representing §6.3.1's
two failure causes were activated through Govee's **official, authenticated
Cloud API** (`POST /device/control`, capability
`devices.capabilities.dynamic_scene`/`lightScene`) directly against the
real device - completely bypassing our own BLE implementation:

- **Aurora** (the `0xFF`-header cause) - activated via cloud, confirmed by
  direct visual observation: **rendered correctly**.
- **Ocean** (the oversized-scene cause, causes an outright BLE disconnect
  in our implementation) - activated via cloud, confirmed by direct visual
  observation: **rendered correctly, no disconnect**.

This is decisive: **both failure modes are bugs in our own BLE
reimplementation of the upload/activation protocol, not defects in the
scene data itself and not a device/firmware limitation.** The device can
clearly handle both scenes correctly when driven by Govee's own pipeline.

Important caveat on how far this generalizes: this device is WiFi-connected,
so the cloud command almost certainly reached it over WiFi/MQTT, not
through the BLE GATT characteristic this whole investigation is built on.
That means this result proves the *destination state is achievable* - not
*how to get there over BLE*. It doesn't hand us the correct bytes; it just
confirms conclusively that correct bytes exist and are worth continuing to
look for, rather than accepting these scenes as inherently BLE-unsupported.

**How this was done** (for reproducing against other scenes): fetch
`https://openapi.api.govee.com/router/api/v1/user/devices` with header
`Govee-API-Key: <key>` to find the device's cloud `device` id (a longer,
differently-formatted ID than the BLE MAC - correlated to our devices via
the last 6 bytes matching the known WiFi MAC, e.g. cloud id
`2D:DB:5C:E7:53:F4:74:56` for the device whose WiFi MAC is
`5C:E7:53:F4:74:56`). Then `POST /device/scenes` with
`{"payload": {"sku": "H60A6", "device": "<cloud id>"}}` to get the
authoritative per-device scene list as `{"name": ..., "value": {"paramId":
X, "id": Y}}` pairs. Then `POST /device/control` with
`{"payload": {"device": "<cloud id>", "sku": "H60A6", "capability":
{"type": "devices.capabilities.dynamic_scene", "instance": "lightScene",
"value": {"paramId": X, "id": Y}}}}` to activate.

**Side finding on scene ID fields** (metadata clarification, not a
protocol fix): the official `paramId`/`id` pair does **not** match the
`sceneCode` this project uses for BLE bare activation (`33 05 04`).
Checked directly for Aurora: official `paramId=28607` matches the public
library's `effect.scenceParamId` field (previously fetched and silently
discarded by `scene_library.py`); official `id=18443` matches the
`scene.sceneId` *outer* field (a different field from `effect.sceneCode`,
which is `19074` for the same scene and is what our BLE opcode actually
uses). These are two independent, parallel ID schemes for the same scene -
cloud/WiFi vs. BLE - and since our BLE `sceneCode` field was reverse
engineered directly from real captured wire bytes (not guessed from the
cloud API), there's no reason to believe switching to the cloud's `id`
would work over BLE; this is just a naming/field-mapping clarification so
future work doesn't confuse the two schemes.

Also confirmed while doing this: both physical devices are visible via
this same official API, and their built-in scene library there is
identical to the public unauthenticated library used throughout this
project (same 84 scene names, same underlying IDs) - independent
confirmation that the scene data source used throughout §6-§10 is correct
and genuinely scoped to this SKU, not a data-sourcing error (this had been
directly questioned and checked before reaching this point - see the
per-SKU filtering verification: querying the same public endpoint with
`sku=H6072` returns a materially different 69-scene list, confirming real
server-side per-SKU scoping).

### 6.6 Alignment check against AlgoClaw/Govee's documented methodology

[AlgoClaw/Govee](https://github.com/AlgoClaw/Govee)'s
[`explanation_v1.2.md`](https://github.com/AlgoClaw/Govee/blob/main/decoded/v1.2/explanation_v1.2.md)
documents a generic, model-agnostic algorithm for building the same kind
of multi-line scene upload this project implements, reverse-engineered
independently across ~26 different Govee SKUs (none of them the H60A6 -
see §11.2). Going through it step by step against this project's own
implementation (`scene_library.build_scene_chunks` and
`client.set_scene`/`set_scene_full`):

| Step (AlgoClaw's terms) | AlgoClaw's rule | This project | Match? |
|---|---|---|---|
| Multi-line prefix | `a3` on every chunk | `0xA3` (`const`/`client.py`) | ✅ identical |
| Header prepended before splitting | `01` + `num_lines` (1 byte each) | `bytes([0x01, chunk_count])` | ✅ identical |
| Line length | 34 hex chars = 17 bytes of payload per line | 17-byte pieces | ✅ identical |
| `num_lines` calculation | `ceil((data_len_bytes + 2) / 17)` (their "+4" is in hex chars = 2 bytes) | `-(-(2 + len(data)) // 17)` | ✅ identical |
| Line index scheme | `00, 01, ..., (num_lines-2)`, last line always `ff` (not `num_lines-1`) | `seq = 0xFF if i == num_pieces-1 else i` | ✅ identical |
| Packet padding | Trailing zeros to 38 hex chars (19 bytes) before checksum | `body + b"\x00"*(19-len(prefix))` | ✅ identical |
| Checksum | 8-bit XOR sum, 1 byte | `_checksum`: XOR of all 19 body bytes | ✅ identical |
| Standard/`modeCmd` command | `330504` + scene code as **byte-swapped** (i.e. little-endian) 2-byte value | `bytes([0x33, 0x05, 0x04, low, high])`, `_scene_id` returns `(code & 0xFF, (code >> 8) & 0xFF)` | ✅ identical (their "convert→split→reverse→combine" walkthrough is just little-endian encoding) |
| `normal_command_suffix` (optional trailing bytes on the standard command, model-specific) | Present for some models (e.g. `0047` for H6065) | Not used - none observed in any real H60A6 capture | ✅ consistent (real captures win over a generic doc for a model not in their table) |
| "On command" (`330101` prefix, only for some models e.g. H6079) | Optional, model-specific | Not used | ✅ consistent (H60A6 never observed needing this) |
| **`hex_prefix_remove` / `hex_prefix_add`** (per-scene-"type" substitution applied to the raw `scenceParam` *before* any of the above) | **Required for many models** - e.g. strip a fixed byte sequence matching the scene's "type" and prepend a different one (`""→"02"` for the H6072 family, `"1200000000"→"04"` for one H6065 type, etc.) | **Not implemented at all** - raw `scenceParam` is used as-is (only the single `0x08` bit flip in byte 0 is applied) | ❓ **unverified for H60A6 - see below** |

**The one real gap, and it's the important one.** Every mechanical step of
our chunking/checksumming/framing matches AlgoClaw's documented algorithm
exactly - this is solid, independently corroborated. The **only** step we
don't implement at all is the per-type prefix substitution, and H60A6
isn't in AlgoClaw's table, so there's no way to check what its correct
`hex_prefix_remove`/`hex_prefix_add` values should be (or confirm it needs
none). This lines up exactly with §6.3.1/§6.5's open mystery: our 3
real-capture-verified references (Graffiti, Christmas, Cornfield) all have
a *different* header signature (`50 54`, `50 42`) from the disputed
scenes (`50 20`), so it's entirely possible the `50 20` family is a
different "type" for H60A6 that needs a prefix substitution we've simply
never had a real capture to derive - i.e. our `0x08` bit-flip fix may be
necessary but not sufficient for that family, rather than a complete fix
that happens to fail for unrelated reasons.

One more honest note: AlgoClaw's methodology has no equivalent of the
`0x08` flag bit at all - their documented transformation is entirely
byte-sequence substitution (remove one prefix, add another), never a
single-bit flip within an otherwise-unmodified byte. Our flag-bit fix
(§6.3) was derived independently from real H60A6 captures and is solidly
confirmed correct for what it covers - but it's worth being clear that
it's not "the H60A6 instance of AlgoClaw's general mechanism," it's a
separate, so-far H60A6-specific finding that sits alongside a still-open
question of whether a *second*, AlgoClaw-style prefix substitution is
also needed for at least the `50 20` scene family.

## 7. Scene library / scene codes

Govee's app fetches the SKU's scene library from a **public, unauthenticated
HTTP endpoint** (only needs an `AppVersion` header):

```
GET https://app2.govee.com/appsku/v1/light-effect-libraries?sku=H60A6
```

Response includes, per scene: `sceneName`, `sceneCode` (16-bit int — the
same value used in the `33 05 04` activation command, little-endian), and
`scenceParam` (base64-encoded effect data — the payload chunked per §6,
after the flag-bit fix). This single endpoint is what makes correct
scene-data upload possible at all without capturing every scene
individually from the real app — confirmed to return 84 scenes for this
SKU, more than the 69 originally captured from the app by hand.

Scene ID families observed (not random — grouped by content type):
- Most named scenes: `0xXX 0x4A` (e.g. Sunrise = `83 4A`).
- Basic/utility scenes ("White Light", "Illumination"): `0xXX 0x00`.
- Licensed content (Moana-themed pack — "Motunui", "Heart of the Island",
  "Wayfinding"): `0xXX 0x5C`.

## 8. Device metadata (`ab` opcode)

A separate opcode family from `ac`, used to query specific device
metadata fields. Request format: `ab 01 <field_id> <zeros>`. Response uses
the same chunked format as `ac`/`a3` (sequence numbers, `0xFF` terminator).

Confirmed field IDs (queried by the real app on every connection, in this
order):

| Field ID | Content | Example |
|---|---|---|
| `0x02` | Short value, meaning not determined (single byte payload, `0x01` observed) | — |
| `0x04` | Long hex-string blob, likely a device certificate/secret for cloud pairing — not firmware version | `1782966739436b879a...` (~140 hex chars) |
| `0x05` | Device serial/UID, ASCII hex string | `F19130565FE741AF` |

**Firmware version was not found anywhere** — not in `ac` status chunks,
not in any `ab` field queried by the app, even with the phone in airplane
mode (network fully disabled, forcing BLE-only operation). The app may
simply display a value cached from a prior cloud sync rather than querying
it fresh. Model (`H60A6`) is a fixed constant, not queried.

## 9. What's confirmed cloud-only (not available over BLE)

Verified by testing with the phone in airplane mode (BLE-only, no cloud
fallback possible) and observing which app features simply don't work at
all offline:

- **Power-off Memory** (whether the light remembers on/off state after a
  power cut) — cloud API only.
- **Device display name** (the user-assigned friendly name shown in the
  app) — cloud API only.

Confirmed **available** over BLE via the same airplane-mode test (the app
UI for these worked with no network): firmware version display (though the
underlying query wasn't captured, see §8), hardware version, MAC address,
model, and calibration.

## 10. Known unresolved items

1. **Scene upload rendering failures** (§6.3/§6.3.1) — the ack-handling
   fix in §6.2.1 was real and necessary (fixed a genuine 36%→83% success
   swing measured via status query), but status-query success does **not**
   mean the scene actually rendered (§6.4) - human-observed testing found
   the automated test's "100% reliable" verdict for Desert and Aurora was
   wrong. Two distinct, confirmed root causes remain, both unresolved:
   - **`0xFF` header placeholder bytes** (§6.3.1 cause 1) - affects at
     least Aurora, Dandelion, Desert, Fall, Green Wheat Field, and Volcano,
     and by the same header signature likely ~12 more of the 84 scenes in
     the public library. Size-independent (fails even at 141 bytes).
     A live A/B experiment tried substituting Aurora's single `0xFF` byte
     with three candidate values (`0x00`, `0x01`, `0x64`) - **inconclusive**,
     because the probe script didn't establish a distinguishable baseline
     first, so the observed light behavior couldn't be attributed to any
     specific variant (likely just showing a previously-cached scene
     throughout). §6.6's methodology comparison against
     [AlgoClaw/Govee](https://github.com/AlgoClaw/Govee) suggests a more
     promising direction than blind byte substitution: their documented
     algorithm (independently verified to match this project's chunking/
     checksums/framing exactly) requires a per-scene-"type"
     `hex_prefix_remove`/`hex_prefix_add` substitution step that this
     project doesn't implement at all - H60A6 isn't in their model table,
     so its correct values (if any) are unknown. This is a more specific,
     structurally-motivated hypothesis than "some byte needs to change,"
     but still needs either a real capture or a properly-controlled live
     experiment (with a verified baseline) to confirm.
   - **Oversized scenes** (§6.3.1 cause 2) - confirmed for Ocean (336
     bytes/20 chunks, also causes an outright BLE disconnect) and Winter
     (368 bytes/22 chunks). Clean headers, size is the only common factor.
     Threshold is somewhere between ~254 bytes (Spring, works) and ~336
     bytes (Ocean, fails) - not pinned down exactly.
   No real capture of the app freshly uploading a scene affected by either
   cause has been obtained (every observed activation reused the device's
   cache). The original "multi-segment sub-block" hypothesis (repeating
   `02 00 64 xx 10 27 00 00 00 00` markers) does not explain either cause
   - marker count doesn't correlate with pass/fail in either group.
   **Update (§6.5): decisively confirmed to be a bug in our own BLE
   implementation, not the scene data or a device limitation** - both
   Aurora and Ocean were activated successfully (visually confirmed) via
   Govee's official Cloud API directly against the real device, completely
   bypassing our BLE code. A fix is known to be possible; the exact BLE-level
   mechanism is still unresolved, since the cloud path almost certainly
   travels over WiFi, not the BLE characteristic this project uses.
2. **Firmware version** — not located anywhere in the BLE protocol
   surface explored so far (§8).
3. **Calibration** — confirmed to exist and be BLE-exposed (app-observable
   rotation adjustment, ±30° tested), but the actual command was not
   captured/decoded.
4. **Power-off Memory / device name** — confirmed cloud-only, not
   achievable via BLE at all (§9), so not actually "unresolved" so much as
   out of scope for a BLE-only implementation.
5. **Zone-state status parsing, any time the two zones differ** (§5.2/
   §5.2.1) — found via a live controlled test, not yet solved, and
   confirmed to have a real, user-visible impact: a "can't turn the light
   on/off" report was traced directly to this bug. Originally believed to
   be specific to "scene mode" (an A/B check with the Cloud API seemed to
   confirm that), but further isolated live testing (single device, no
   HA/adapter contention, delays up to 2s) disproved that scoping - the
   same misreporting reproduces in a completely normal, non-scene state,
   any time the two zones simply differ from each other, which is
   essentially every ordinary on/off toggle. The zone *commands*
   themselves are solid (verified by direct physical observation early in
   this investigation), but the status query's *report* of zone on/off
   state uses an encoding more complex than the "two independent 0/1 flag
   bytes" model that happened to work for every previously-captured
   fixture (which never varied the two zones independently) - one test
   even showed a command targeting one zone appearing to change the
   *other* zone's reported state, which rules out both the original model
   and a simple "upper/lower swapped" theory. Do not trust
   `zone_upper_on`/`zone_lower_on` from a live status query as reliable
   whenever the two zones might differ, full stop - not just in scene
   mode. **Mitigated in the UI** (not fixed at the protocol level): `light.py`
   and `switch.py` now track on/off optimistically from the last command
   sent rather than trusting this decode, the same pattern already used
   for RGB/color-temp. The actual byte-level fix is still needed for
   anything that depends on truly reading zone state (e.g. detecting an
   external change from the Govee app) and remains unresolved.

### 10.1 What live device testing (`test_live_device.py`) confirmed works

For contrast with the above, an actual connect-and-run-every-command test
against a real device (not just static fixture comparison) confirmed
these round-trip correctly end-to-end, command  then status-query
readback:

- Brightness set + readback (both a mid-range and 100% value).
- Bare scene activation (`33 05 04`) + readback of the resulting scene ID.
- Full scene upload (`a3` chunks with the flag-bit fix) + activation +
  readback, for a small scene (Graffiti) - the device actually rendered
  it, not just acked it.
- RGB color set, with status parsing (MAC/hardware version) remaining
  correct afterward - the specific regression this was checking for.
- Color temperature set, same regression check.

The zone-state issue above was the only thing this uncovered that the
static tests had missed; everything else that could be checked without
visual confirmation checked out.

## 11. Prior art / cross-referenced sources

None of these test the H60A6 directly; they cover other Govee product
generations and are useful for corroboration, not ground truth for this
SKU:

- [wcbonner/GoveeBTTempLogger](https://github.com/wcbonner/GoveeBTTempLogger) —
  source of the AES-ECB+RC4 encryption scheme and PSK (built for Govee
  thermometers, a different product line entirely).
- [egold555/Govee-Reverse-Engineering issue #11](https://github.com/egold555/Govee-Reverse-Engineering/issues/11) —
  independently documents the `33 05 04 <scene_code>` activation format
  for a different device (matches exactly).
- [BeauJBurroughs/Govee-H6127-Reverse-Engineering](https://github.com/BeauJBurroughs/Govee-H6127-Reverse-Engineering),
  [KunaalKumar/Govee-H6072-Reverse-Engineering](https://github.com/KunaalKumar/Govee-H6072-Reverse-Engineering) —
  confirm `33 05 15 01` as a real, recognized opcode prefix across Govee's
  product line (documented there as a "switch to color mode" trigger sent
  separately before color data, vs. combined into one packet on the
  H60A6).
- [wez/govee2mqtt](https://github.com/wez/govee2mqtt) —
  independent Rust implementation of scene-chunk encoding; its exact byte
  framing does **not** match this device (verified: 63/102 bytes differ
  vs. 3/102 for the framing documented in §6 here), so likely applies to a
  different SKU or is itself unverified — not something to blindly adopt.
- [Beshelmek/govee_ble_lights](https://github.com/Beshelmek/govee_ble_lights) —
  a real, actively-maintained HA custom integration for many Govee BLE
  light SKUs (does not include the H60A6, or any H60-series device;
  supports mostly strip/bulb models like H6072, H6199, H61A0, etc.). Its
  source was fetched and read directly (`govee_utils.py`, `light.py`,
  `govee_api.py`). Useful as genuine independent corroboration of parts of
  our own command-side reverse engineering:
  - Its generic multi-chunk packet builder (`prepareMultiplePacketsData`)
    uses the **same header layout** we independently derived for `a3`
    scene uploads: `[opcode, seq, constant=0x01, chunk_count, data...]`.
    Different project, same structure - strong confirmation this framing
    is correct, not a coincidence of how we happened to interpret our own
    captures.
  - Defines `LedMode.SEGMENTS = 0x15`, confirming `33 05 15 01` (§4) is
    genuinely Govee's "SEGMENTS" mode opcode, not a meaning we guessed at.
  - Tempers one hope, though: even on its own explicitly segmented models,
    the byte after `0x15` is hardcoded to `0x01` rather than used as a
    real per-segment index - i.e. this does not reveal a mechanism for
    addressing individual segments beyond our existing upper/lower zone
    split.
  - Its scene-upload call site passes an extra one-byte header (`0x02`)
    ahead of the `scenceParam` payload that our verified-working H60A6
    implementation does not need - another confirmed real per-device
    protocol variation (consistent with that project's own README:
    "almost every Govee device has its own BLE message protocol").
  - Has **no status/notification-reading code anywhere** - it is
    command/write-only, like every other source found so far. It does not
    bear on the zone-state status-parsing mystery in §5.2 at all; its
    `get_device_state` function is the unrelated official Govee cloud
    "Open API," not BLE.
- [grantwhitney3/govee-scenes](https://github.com/grantwhitney3/govee-scenes) -
  the repo itself is thin (a config-driven scene-application script, no
  scene decoding or SKU validation of its own), but it depends on a PyPI
  package, `govee-python` (imported as `govee`), that turned out to be
  genuinely useful - it implements the official, authenticated Govee Cloud
  API in full, including a per-device scene endpoint
  (`POST /device/scenes`) this project hadn't used before (everything
  else here comes from the public, unauthenticated, SKU-only
  `app2.govee.com` library). Used directly (with a real API key) to
  perform the decisive test in §6.5 - activating scenes via Govee's own
  pipeline to determine whether §6.3.1's failures were bugs in our BLE
  code or in the scene data itself (confirmed: our BLE code). Also
  surfaced the `paramId`/`id` field-naming clarification in §6.5.

### 11.1 Searched for and did not find

Explicitly checked and found **no prior art** for the zone-state encoding
quirk in §5.2, or for the mode-dependent status layout in §5.1 generally:

- [wez/govee2mqtt issue #409](https://github.com/wez/govee2mqtt/issues/409) —
  the H60A6 support request/discussion thread itself (30 comments). Useful
  as confirmation that **no one else has independent zone/segment control
  working for this device at all** as of this writing — that community is
  entirely blocked on Govee's cloud API exposing it (a fundamentally
  different mechanism than the direct-BLE approach used here), and their
  best available workaround is the same DIY/custom-effect trick
  independently discovered in this investigation (§6, Finger Sketch). No
  byte-level protocol detail of any kind is present in that thread.
- General BLE reverse-engineering resources confirm packed multi-bit
  status flags (as opposed to one-flag-per-byte) are a common pattern in
  BLE protocols generally, which is consistent with what §5.2 found, but
  nothing Govee- or device-specific.
- [Bluetooth-Devices/govee-ble](https://github.com/Bluetooth-Devices/govee-ble) —
  checked directly (source pulled and read); exclusively covers Govee's
  sensor line (thermometers, motion/window/vibration/button/pressure
  sensors), parsing passive advertisement broadcasts rather than
  connection-based GATT status responses. Different device category,
  different transport pattern, no overlap.
- [Beshelmek/govee_ble_lights](https://github.com/Beshelmek/govee_ble_lights) —
  see the corroboration entry above; genuinely useful for command framing,
  but has no status-read code at all, so contributes nothing to this
  specific mystery either.

Net conclusion: the zone-state encoding, and the mode-dependent status
layout more broadly, appear to be genuinely undocumented anywhere public.
This isn't a case of having missed an existing answer - it needs to be
solved the same way everything else in this document was, through our own
controlled experiments.

### 11.2 Searched for and did not find: internal scene-data byte semantics

Separately, following a lead from the official
[Govee LAN API guide](https://app-h5.govee.com/user-manual/wlan-guide)
(client-rendered SPA, no static content - the guide itself only documents
four basic LAN commands: `turn`, `brightness`, `devStatus`, `colorwc`; no
scene/effect mechanism exists over LAN at all, for any Govee device, which
is presumably *why* every community project - including this one - relies
on either BLE or the authenticated Cloud API for scenes), a broader search
was made for any documentation of what the bytes *inside* a `scenceParam`
blob actually mean (as opposed to the outer multi-chunk wrapping mechanism,
which is well covered - see §6.1/§11):

- [egold555/Govee-Reverse-Engineering issue #11](https://github.com/egold555/Govee-Reverse-Engineering/issues/11) -
  read in full (all comments). Documents scene *code* extraction and the
  outer chunking mechanism only. No explanation of what the color/timing/
  flag bytes inside the blob represent.
- [AlgoClaw/Govee](https://github.com/AlgoClaw/Govee) - by far the most
  substantial community resource found for this question. Documents a
  generic outer-wrapping algorithm (see §6.6 for a full field-by-field
  comparison against this project's own implementation) plus a per-model
  `hex_prefix_remove`/`hex_prefix_add` substitution table
  (`model_specific_parameters.json`) for handling model-specific quirks in
  that wrapping. Checked all ~26 models' raw scene data in the repo
  (`raw/withparams/*.json`) for any scene sharing the H60A6's `50 20...`
  header signature - **none exist**. Every model in that dataset uses a
  different internal format (`03`, `05`, `01`-prefixed, etc.). H60A6 is not
  in the model table at all - nobody has reverse engineered its specific
  `hex_prefix_remove`/`hex_prefix_add` values (if it even needs them).
- **H60A1** (the non-Pro sibling of the H60A6, same ceiling-light product
  line - directly relevant since the account used throughout this
  investigation owns two of them) - checked its public scene library
  directly (`sku=H60A1`) expecting the closest possible format match.
  It isn't: H60A1's scene headers start with `03`, `05`, `02`, `01` -
  a completely different internal format from H60A6's `50 20...`, despite
  being the same physical product line. Even the closest sibling doesn't
  share this format, which rules out "look up a sibling model" as a
  shortcut.
- General web search for scenceParam/keyframe decoding turned up nothing
  beyond the sources already cited throughout §6/§11.

Net conclusion, consistent with §11.1's finding for the zone-state mystery:
**the internal semantic structure of Govee scene effect data (what each
header byte controls, what a literal `0xFF` in that structure means) is
not publicly documented anywhere, for any Govee device.** Every community
resource found documents *transmission* mechanics (chunking, checksums,
per-device wrapping quirks) for replaying pre-existing template data, not
the *meaning* of the data itself. This isn't a gap specific to how this
project searched - it appears to be a gap in the entire public
reverse-engineering corpus for this product family.
