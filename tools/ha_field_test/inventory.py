"""Read-only inventory: every govee_ble_local device Home Assistant has, as the plan needs it.

:func:`fetch_raw` makes the reads (WebSocket registries, states, Bluetooth scanner details
and advertisement history, REST diagnostics); nothing it calls changes anything.
:func:`parse_inventory` turns them into :class:`DeviceDesc` records and is pure, so the
tests build device descriptions without Home Assistant.

An entity's role comes from its unique id, ``{address}_{key}`` (``entity.py``): ``light``
is the whole-fixture light, ``zone_<name>_light`` a zone light, ``segment_<i>`` a segment
light, ``zone_<name>`` a zone switch, ``power`` a relay plug's switch, ``self_test`` the
self-test button, ``connection_source`` the sensor naming the scanner Home Assistant
prefers for the device. Capabilities come from the entity's own state attributes (what
Home Assistant offers) and the config entry's diagnostics (what the library says), never
from a per-model table.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any

from .masking import Masker

if TYPE_CHECKING:
    from .ha_api import RestClient, WebSocketClient

DOMAIN = "govee_ble_local"
ROLE_FIXTURE = "light"
ROLE_ZONE_LIGHT = "zone_light"
ROLE_SEGMENT = "segment"
ROLE_ZONE_SWITCH = "zone_switch"
ROLE_PLUG = "plug"
ROLE_SELF_TEST = "self_test"
ROLE_SOURCE = "connection_source"
ROLE_OTHER = "other"
UNAVAILABLE = frozenset({"unavailable", "unknown"})
_ZONE_LIGHT = re.compile(r"^zone_(.+)_light$")
_ZONE = re.compile(r"^zone_(.+)$")
_SEGMENT = re.compile(r"^segment_(\d+)$")
# A Govee broadcast: company id 0x88xx (the header byte low) and a value starting 0xEC
# (identify.py in govee-ble-local); or a name in one of the shapes Govee advertises.
_GOVEE_NAME = re.compile(r"^(GV[HR]?[0-9A-Z]|ihoment_|Govee_|Minger_|GBK_)")
_GOVEE_SKU = re.compile(r"(H[0-9][0-9A-Z]{3})")
_GOVEE_COMPANY_HI = 0x88
# Attributes of a light or switch that make up its state, for the journal and the verdicts.
STATE_ATTRIBUTES = (
    "color_mode",
    "brightness",
    "rgb_color",
    "color_temp_kelvin",
    "effect",
)
CAPABILITY_ATTRIBUTES = (
    "supported_color_modes",
    "min_color_temp_kelvin",
    "max_color_temp_kelvin",
    "effect_list",
    "supported_features",
)


def snapshot_of(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return an HA state object's state and the attributes that make it up."""
    if state is None:
        return {"state": None}
    attrs: Mapping[str, Any] = state.get("attributes") or {}
    return {"state": state.get("state"), **{k: attrs[k] for k in STATE_ATTRIBUTES if k in attrs}}


@dataclass(frozen=True)
class EntityDesc:
    """One entity of a device, with its state when read."""

    entity_id: str
    domain: str
    role: str
    key: str
    enabled: bool
    state: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    zone: str | None = None
    segment: int | None = None

    @property
    def available(self) -> bool:
        """Whether the entity has a usable state."""
        return self.enabled and self.state is not None and self.state not in UNAVAILABLE

    def snapshot(self) -> dict[str, Any]:
        """Return the entity's state and the attributes that make it up (for the journal)."""
        return {
            "state": self.state,
            **{k: self.attributes.get(k) for k in STATE_ATTRIBUTES if k in self.attributes},
        }


@dataclass(frozen=True)
class DeviceDesc:
    """One govee_ble_local device: identity (private), capabilities, entities, start state."""

    label: str
    sku: str
    entry_id: str
    title: str
    names: tuple[str, ...]
    address: str
    area: str | None
    entry_state: str
    capabilities: frozenset[str] = frozenset()
    zones: tuple[str, ...] = ()
    scene_count: int | None = None
    library_state: Mapping[str, Any] = field(default_factory=dict)
    entities: tuple[EntityDesc, ...] = ()
    source: str | None = None
    source_name: str | None = None  # the scanner's real name (private: the stop rule's match)
    hex_ids: tuple[str, ...] = ()
    diagnostics_error: str | None = None
    # The neutral labels the reports use (masking.py): never the area's or scanner's name.
    area_label: str | None = None
    source_label: str | None = None
    # Every scanner heard advertising it during the inventory (by address): a connect can
    # take any of them, whatever the Connection source sensor shows.
    heard_by: frozenset[str] = frozenset()

    def role(self, role: str) -> list[EntityDesc]:
        """Return the device's entities of ``role``, in a stable order."""
        found = [e for e in self.entities if e.role == role]
        return sorted(found, key=lambda e: (e.segment if e.segment is not None else -1, e.key))

    @property
    def fixture(self) -> EntityDesc | None:
        """The whole-fixture light, if the device has one."""
        return next(iter(self.role(ROLE_FIXTURE)), None)

    @property
    def plug(self) -> EntityDesc | None:
        """The relay plug's power switch, if the device is a plug."""
        return next(iter(self.role(ROLE_PLUG)), None)

    @property
    def outlets(self) -> list[EntityDesc]:
        """A per-outlet relay device's outlet switches: zone switches and no fixture light.

        Since the integration's 1.7 zone rework, zone switches exist only on such a device
        (``helpers.has_zone_lights``): it is a plug, and needs the user's consent too.
        """
        return [] if self.fixture is not None else self.role(ROLE_ZONE_SWITCH)

    @property
    def is_plug(self) -> bool:
        """Whether the device switches mains power: a relay plug, or a per-outlet relay."""
        return self.plug is not None or bool(self.outlets)

    @property
    def main(self) -> EntityDesc | None:
        """The entity that stands for the whole device (the fixture, the plug or an outlet)."""
        return self.fixture or self.plug or next(iter(self.outlets), None)

    @property
    def optimistic(self) -> bool:
        """Whether the library tracks the device's state without reading it back."""
        return bool(self.library_state.get("optimistic", False))

    @property
    def available(self) -> bool:
        """Whether the device can be tested: loaded, and its main entity has a state."""
        main = self.main
        return self.entry_state == "loaded" and main is not None and main.available

    def fragile(self, sources: frozenset[str]) -> bool:
        """Whether a connect to the device may go through one of the fragile ``sources``."""
        return self.source in sources or bool(self.heard_by & sources)


@dataclass
class Inventory:
    """Everything the plan needs, plus what can't be tested on this route and why."""

    devices: list[DeviceDesc]
    not_testable: list[tuple[str, str]]
    unmanaged: list[tuple[str, str]]
    scanners: list[dict[str, Any]]
    ha_version: str | None
    integration_version: str | None
    masker: Masker
    requirements: list[str] = field(default_factory=list)
    scanner_labels: dict[str, str] = field(default_factory=dict)
    discovered: dict[str, list[str]] = field(default_factory=dict)
    discovered_error: str | None = None

    def device(self, label: str) -> DeviceDesc | None:
        """Return the device with ``label``."""
        return next((d for d in self.devices if d.label == label), None)


@dataclass
class RawInventory:
    """What :func:`fetch_raw` read from Home Assistant, unparsed."""

    entries: list[dict[str, Any]]
    devices: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    areas: list[dict[str, Any]]
    states: dict[str, dict[str, Any]]
    scanners: list[dict[str, Any]]
    diagnostics: dict[str, dict[str, Any] | str]
    adverts: list[dict[str, Any]]
    ha_version: str | None = None
    # Each scanner's own discovered devices (the Bluetooth integration's diagnostics):
    # {scanner address: [device address, ...]}, not deduplicated across scanners.
    discovered: dict[str, list[str]] = field(default_factory=dict)
    discovered_error: str | None = None  # why they couldn't be read, if they couldn't


def entity_role(  # noqa: PLR0911 - one return per role
    domain: str, key: str
) -> tuple[str, str | None, int | None]:
    """Return (role, zone name, segment index) for an entity's unique-id key."""
    if key == "light" and domain == "light":
        return ROLE_FIXTURE, None, None
    if domain == "light" and (m := _ZONE_LIGHT.match(key)):
        return ROLE_ZONE_LIGHT, m.group(1), None
    if domain == "light" and (m := _SEGMENT.match(key)):
        return ROLE_SEGMENT, None, int(m.group(1))
    if domain == "switch" and key == "power":
        return ROLE_PLUG, None, None
    if domain == "switch" and (m := _ZONE.match(key)):
        return ROLE_ZONE_SWITCH, m.group(1), None
    if domain == "button" and key == "self_test":
        return ROLE_SELF_TEST, None, None
    if domain == "sensor" and key == "connection_source":
        return ROLE_SOURCE, None, None
    return ROLE_OTHER, None, None


def _device_address(device: Mapping[str, Any]) -> str | None:
    return next(
        (str(v) for d, v in device.get("identifiers") or () if d == DOMAIN),
        None,
    )


def _device_entry(device: Mapping[str, Any]) -> str | None:
    """Return the config entry of a device-registry record (2026.10 keeps both forms)."""
    entry = device.get("primary_config_entry") or device.get("config_entry_id")
    if entry:
        return str(entry)
    entries = device.get("config_entries") or ()
    return str(entries[0]) if entries else None


def _entities_of(
    device_id: str, address: str, raw: RawInventory
) -> tuple[list[EntityDesc], str | None]:
    """Return a device's entities and its connection source (the scanner's address)."""
    found: list[EntityDesc] = []
    source: str | None = None
    for reg in raw.entities:
        if reg.get("platform") != DOMAIN or reg.get("device_id") != device_id:
            continue
        unique_id = str(reg.get("unique_id", ""))
        key = unique_id[len(address) + 1 :] if unique_id.startswith(f"{address}_") else unique_id
        entity_id = str(reg["entity_id"])
        domain = entity_id.split(".", 1)[0]
        role, zone, segment = entity_role(domain, key)
        state = raw.states.get(entity_id)
        if role == ROLE_SOURCE and state and state.get("state") not in UNAVAILABLE:
            source = str(state["state"])
        if role == ROLE_OTHER:
            continue
        found.append(
            EntityDesc(
                entity_id=entity_id,
                domain=domain,
                role=role,
                key=key,
                enabled=reg.get("disabled_by") is None,
                state=None if state is None else str(state.get("state")),
                attributes=dict(state.get("attributes") or {}) if state else {},
                zone=zone,
                segment=segment,
            )
        )
    return found, source


def _diagnostics_parts(
    diag: Mapping[str, Any] | str | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any], str | None]:
    """Return (device part, state part, error) of an entry's diagnostics download."""
    if not isinstance(diag, Mapping):
        return {}, {}, diag or "no diagnostics"
    data = diag.get("data") or {}
    return data.get("device") or {}, data.get("state") or {}, None


def integration_version(raw: RawInventory) -> tuple[str | None, list[str]]:
    """Return the integration's installed version and requirements, from its diagnostics.

    The requirement shows which library build Home Assistant was told to install (a
    release candidate's wheel URL while one is under test).
    """
    for diag in raw.diagnostics.values():
        if isinstance(diag, Mapping):
            component = (diag.get("custom_components") or {}).get(DOMAIN) or {}
            if version := component.get("version"):
                return str(version), [str(r) for r in component.get("requirements") or ()]
    return None, []


_WHEEL_VERSION = re.compile(r"govee_ble_local-([0-9][^-]*)-")
_PINNED_VERSION = re.compile(r"(==|>=)\s*([0-9][^,;\s]*)")


def library_version(requirements: list[str]) -> str | None:
    """Return the library version the integration's requirement names, if it names one.

    A release candidate's wheel URL names its version in the file name; a specifier its
    ``==`` pin or ``>=`` floor. A floor alone doesn't say what is installed: the runner
    has the user confirm that (runbook step 2's read-only check).
    """
    for requirement in requirements:
        if not requirement.replace("_", "-").lower().startswith("govee-ble-local"):
            continue
        if m := _WHEEL_VERSION.search(requirement):
            return m.group(1)
        if m := _PINNED_VERSION.search(requirement):
            return m.group(2)
    return None


def is_govee_advert(advert: Mapping[str, Any]) -> bool:
    """Whether an advertisement is a Govee device's (its broadcast marker, or a Govee name)."""
    for company, value in (advert.get("manufacturer_data") or {}).items():
        if (
            str(company).isdigit()
            and int(company) >> 8 == _GOVEE_COMPANY_HI
            and str(value).lower().startswith("ec")
        ):
            return True
    return bool(_GOVEE_NAME.match(str(advert.get("name") or "")))


def _sku_in(text: str) -> str:
    return m.group(1) if (m := _GOVEE_SKU.search(text)) else "unknown model"


def _unmanaged(
    raw: RawInventory, managed: set[str], masker: Masker, labels: Mapping[str, str]
) -> list[tuple[str, str]]:
    """Return the Govee devices Home Assistant hears that no govee_ble_local entry has.

    Each is named by its model and a number (``H61C3 (heard #1)``), never its address or
    name, which are registered with the masker under that label.
    """
    heard: dict[str, Mapping[str, Any]] = {}
    for advert in raw.adverts:
        address = str(advert.get("address", "")).upper()
        if address and address not in managed and is_govee_advert(advert):
            heard[address] = advert
    out: list[tuple[str, str]] = []
    for n, address in enumerate(sorted(heard), 1):
        name = str(heard[address].get("name") or "")
        label = f"{_sku_in(name)} (heard #{n})"
        masker.add(label, names=(name,), hex_ids=(address,))
        scanner = labels.get(str(heard[address].get("source")), "an unknown scanner")
        out.append((label, f"heard through {scanner}, no govee_ble_local entry"))
    return out


def _heard_by(raw: RawInventory) -> dict[str, frozenset[str]]:
    """Return, by device address, every scanner that has heard it.

    From each scanner's own discovered devices in the Bluetooth integration's diagnostics
    (Home Assistant's advertisement history keeps one scanner per device), and from the
    advertisements collected meanwhile.
    """
    heard: dict[str, set[str]] = {}
    for source, addresses in raw.discovered.items():
        for address in addresses:
            heard.setdefault(address.upper(), set()).add(source)
    for advert in raw.adverts:
        if advert.get("source"):
            heard.setdefault(str(advert.get("address", "")).upper(), set()).add(
                str(advert["source"])
            )
    return {address: frozenset(sources) for address, sources in heard.items()}


def _discovered(ws: WebSocketClient, rest: RestClient) -> tuple[dict[str, list[str]], str | None]:
    """Read each scanner's discovered devices from the Bluetooth integration's diagnostics.

    Returns them, and why they couldn't be read (then the runner takes the fragile proxy to
    reach every device).
    """
    for entry in ws.command({"type": "config_entries/get", "domain": "bluetooth"}):
        if entry.get("state") != "loaded":
            continue
        try:
            manager = (rest.diagnostics(entry["entry_id"]).get("data") or {}).get("manager") or {}
        except Exception as err:  # noqa: BLE001 - the runner falls back to the safe side
            return {}, f"the Bluetooth diagnostics couldn't be read ({type(err).__name__})"
        return {
            str(scanner["source"]): [
                str(d.get("address"))
                for d in scanner.get("discovered_devices_and_advertisement_data") or ()
                if isinstance(d, Mapping) and d.get("address")
            ]
            for scanner in manager.get("scanners") or ()
            if isinstance(scanner, Mapping) and scanner.get("source")
        }, None
    return {}, "no loaded Bluetooth config entry"


def scanner_name(scanners: list[dict[str, Any]], source: object) -> str | None:
    """Return the name Home Assistant's Bluetooth stack gives the scanner ``source`` (private)."""
    if source is None:
        return None
    return next(
        (str(s.get("name")) for s in scanners if s.get("source") == source),
        str(source),
    )


def scanner_labels(scanners: list[dict[str, Any]]) -> dict[str, str]:
    """Return each scanner's neutral label by its address: ``local adapter``, ``proxy A``, ...

    Local adapters first, then remote proxies, each kind in the order of its address, so a
    scanner keeps its label from one run to the next while the set of scanners stays.
    """
    local = sorted(str(s["source"]) for s in scanners if s.get("scanner_type") != "remote")
    remote = sorted(str(s["source"]) for s in scanners if s.get("scanner_type") == "remote")
    labels = {
        source: "local adapter" if len(local) == 1 else f"local adapter {n}"
        for n, source in enumerate(local, 1)
    }
    for n, source in enumerate(remote):
        labels[source] = f"proxy {chr(ord('A') + n)}" if n < 26 else f"proxy {n + 1}"  # noqa: PLR2004
    return labels


def _register_scanners(
    masker: Masker, scanners: list[dict[str, Any]], labels: Mapping[str, str]
) -> None:
    for scanner in scanners:
        label = labels[str(scanner["source"])]
        masker.add(
            label,
            names=(scanner.get("name"), scanner.get("adapter")),
            hex_ids=(scanner.get("source"),),
        )


def _area_labels(
    raw: RawInventory, devices: list[Mapping[str, Any]], masker: Masker
) -> dict[str, str]:
    """Return each area's label (``area 1``, ...) by area id, in the order of the ids."""
    names = {str(a["area_id"]): str(a.get("name") or a["area_id"]) for a in raw.areas}
    used = sorted({str(d["area_id"]) for d in devices if d.get("area_id")})
    labels = {area_id: f"area {n}" for n, area_id in enumerate(used, 1)}
    for area_id, label in labels.items():
        masker.add_area(label, names.get(area_id), area_id)
    return labels


def _not_testable(
    entries: Mapping[str, Mapping[str, Any]], by_entry: Mapping[str | None, Mapping[str, Any]]
) -> tuple[list[tuple[str, str, str]], set[str]]:
    """Return the entries that can't be tested ((title, model, why)) and the ones that can."""
    out: list[tuple[str, str, str]] = []
    usable: set[str] = set()
    for entry_id, entry in entries.items():
        title = str(entry.get("title") or "")
        device = by_entry.get(entry_id)
        model = str(device.get("model")) if device and device.get("model") else _sku_in(title)
        if entry.get("source") == "ignore":
            out.append((title, model, "discovery ignored in Home Assistant"))
        elif entry.get("disabled_by"):
            out.append((title, model, f"config entry disabled ({entry['disabled_by']})"))
        elif device is None:
            out.append((title, model, f"no device record (entry {entry.get('state')})"))
        else:
            usable.add(entry_id)
    return out, usable


def _draft(
    entry_id: str,
    entry: Mapping[str, Any],
    device: Mapping[str, Any],
    raw: RawInventory,
    areas: Mapping[str, str],
) -> dict[str, Any]:
    """Return one device's fields, before it has its label."""
    title = str(entry.get("title") or "")
    address = _device_address(device) or ""
    dev_part, state_part, diag_error = _diagnostics_parts(raw.diagnostics.get(entry_id))
    entities, source = _entities_of(str(device["id"]), address, raw)
    area_id = str(device.get("area_id")) if device.get("area_id") else None
    area_names = {str(a["area_id"]): str(a.get("name") or a["area_id"]) for a in raw.areas}
    return {
        "sku": str(dev_part.get("sku") or device.get("model") or "H????").upper(),
        "entry_id": entry_id,
        "title": title,
        "names": tuple(n for n in (title, device.get("name"), device.get("name_by_user")) if n),
        "address": address,
        "area": area_names.get(area_id) if area_id else None,
        "area_label": areas.get(area_id) if area_id else None,
        "entry_state": str(entry.get("state")),
        "capabilities": frozenset(str(c) for c in dev_part.get("capabilities") or ()),
        "zones": tuple(str(z) for z in dev_part.get("zones") or ()),
        "scene_count": dev_part.get("scene_count"),
        "library_state": dict(state_part),
        "entities": tuple(entities),
        "source": source,
        "source_name": scanner_name(raw.scanners, source),
        "hex_ids": tuple(
            str(v)
            for v in (
                address,
                device.get("serial_number"),
                *(c[1] for c in device.get("connections") or () if len(c) > 1),
            )
            if v
        ),
        "diagnostics_error": diag_error,
    }


def parse_inventory(raw: RawInventory) -> Inventory:
    """Build the inventory from what :func:`fetch_raw` read (pure)."""
    entries = {str(e["entry_id"]): e for e in raw.entries if e.get("domain") == DOMAIN}
    by_entry = {_device_entry(d): d for d in raw.devices if _device_address(d)}
    excluded, usable = _not_testable(entries, by_entry)
    masker = Masker()
    sources = scanner_labels(raw.scanners)
    _register_scanners(masker, raw.scanners, sources)
    areas = _area_labels(raw, [by_entry[e] for e in usable], masker)
    drafts = [_draft(e, entries[e], by_entry[e], raw, areas) for e in usable]
    drafts.sort(key=lambda d: (d["sku"], d["address"]))
    heard = _heard_by(raw)
    counts: dict[str, int] = {}
    devices: list[DeviceDesc] = []
    for draft in drafts:
        counts[draft["sku"]] = counts.get(draft["sku"], 0) + 1
        label = f"{draft['sku']} #{counts[draft['sku']]}"
        # Entry and entity ids too: Home Assistant's error text can carry them, and an
        # entity id renamed since holds a name the device's names don't.
        ids = [e.entity_id for e in draft["entities"]]
        masker.add(
            label,
            names=(*draft["names"], draft["entry_id"], *ids, *(i.split(".", 1)[1] for i in ids)),
            hex_ids=draft["hex_ids"],
        )
        source_label = sources.get(draft["source"] or "", "an unknown scanner")
        devices.append(
            DeviceDesc(
                label=label,
                source_label=source_label if draft["source"] else None,
                heard_by=heard.get(draft["address"].upper(), frozenset()),
                **draft,
            )
        )
    not_testable: list[tuple[str, str]] = []
    for n, (title, model, why) in enumerate(excluded, 1):
        label = f"{model} (entry #{n})"
        masker.add(label, names=(title,))
        not_testable.append((label, why))
    for desc in devices:
        if not desc.available:
            why = (
                f"config entry {desc.entry_state}"
                if desc.entry_state != "loaded"
                else "unavailable (not reachable now)"
            )
            not_testable.append((desc.label, why))
    managed = {d.address.upper() for d in devices}
    version, requirements = integration_version(raw)
    return Inventory(
        devices=devices,
        not_testable=not_testable,
        unmanaged=_unmanaged(raw, managed, masker, sources),
        scanners=raw.scanners,
        ha_version=raw.ha_version,
        integration_version=version,
        requirements=requirements,
        masker=masker,
        scanner_labels=sources,
        discovered=raw.discovered,
        discovered_error=raw.discovered_error,
    )


def fetch_raw(ws: WebSocketClient, rest: RestClient, *, scan_seconds: float = 10.0) -> RawInventory:
    """Read everything the inventory needs from Home Assistant. Read-only."""
    entries = ws.command({"type": "config_entries/get", "domain": DOMAIN})
    diagnostics: dict[str, dict[str, Any] | str] = {}
    for entry in entries:
        if entry.get("state") != "loaded":
            continue
        try:
            diagnostics[entry["entry_id"]] = rest.diagnostics(entry["entry_id"])
        except Exception as err:  # noqa: BLE001 - one entry's diagnostics never stop the inventory
            diagnostics[entry["entry_id"]] = f"{type(err).__name__}: {err}"
    scanner_events = ws.subscribe_snapshot({"type": "bluetooth/subscribe_scanner_details"})
    discovered, discovered_error = _discovered(ws, rest)
    advert_events = (
        ws.subscribe_snapshot({"type": "bluetooth/subscribe_advertisements"}, settle=scan_seconds)
        if scan_seconds > 0
        else []
    )
    return RawInventory(
        entries=entries,
        devices=ws.command({"type": "config/device_registry/list"}),
        entities=ws.command({"type": "config/entity_registry/list"}),
        areas=ws.command({"type": "config/area_registry/list"}),
        states={s["entity_id"]: s for s in ws.command({"type": "get_states"})},
        scanners=[s for e in scanner_events for s in (e or {}).get("add") or ()],
        diagnostics=diagnostics,
        adverts=[a for e in advert_events for a in (e or {}).get("add") or ()],
        ha_version=ws.ha_version,
        discovered=discovered,
        discovered_error=discovered_error,
    )


def refresh_device(device: DeviceDesc, ws: WebSocketClient, rest: RestClient) -> DeviceDesc:
    """Re-read one device's entity states and diagnostics state (just before its test)."""
    states = {s["entity_id"]: s for s in ws.command({"type": "get_states"})}
    entities = tuple(
        EntityDesc(
            entity_id=e.entity_id,
            domain=e.domain,
            role=e.role,
            key=e.key,
            enabled=e.enabled,
            state=str(states[e.entity_id]["state"]) if e.entity_id in states else None,
            attributes=dict(states[e.entity_id].get("attributes") or {})
            if e.entity_id in states
            else {},
            zone=e.zone,
            segment=e.segment,
        )
        for e in device.entities
    )
    source = next(
        (
            str(states[e.entity_id]["state"])
            for e in device.entities
            if e.role == ROLE_SOURCE and e.entity_id in states
        ),
        device.source,
    )
    _, state_part, diag_error = _diagnostics_parts(rest.diagnostics(device.entry_id))
    return DeviceDesc(
        **{
            **device.__dict__,
            "entities": entities,
            "library_state": dict(state_part) or device.library_state,
            "source": source if source not in UNAVAILABLE else None,
            "diagnostics_error": diag_error,
        }
    )
