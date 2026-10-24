"""Device descriptions for the field-test harness tests, built without Home Assistant.

Every identifier here is made up: MACs are from the RFC 7042 documentation range
(00:00:5E:00:53:xx), names and areas are invented. Never put a real one in a test.
"""

from __future__ import annotations

from typing import Any

from tools.ha_field_test.inventory import (
    ROLE_FIXTURE,
    ROLE_PLUG,
    ROLE_SEGMENT,
    ROLE_SELF_TEST,
    ROLE_ZONE_LIGHT,
    ROLE_ZONE_SWITCH,
    DeviceDesc,
    EntityDesc,
)

ADDRESS = "00:00:5E:00:53:01"
SERIAL = "00:00:5E:00:53:F1:AA:01"
TITLE = "GVH60A6ABCD"
USER_NAME = "Test Light"
AREA = "Test Room"
LOCAL_ADAPTER = "00:00:5E:00:53:A1"
LOCAL_ADAPTER_NAME = f"hci0 ({LOCAL_ADAPTER})"
RGB_CT = ("color_temp", "rgb")
SCENES = ("off", "Sunrise", "Aurora")


def light(
    key: str = "light",
    *,
    role: str = ROLE_FIXTURE,
    state: str = "on",
    modes: tuple[str, ...] = RGB_CT,
    span: tuple[int, int] | None = (2700, 6500),
    effects: tuple[str, ...] | None = SCENES,
    enabled: bool = True,
    zone: str | None = None,
    segment: int | None = None,
    **attrs: Any,
) -> EntityDesc:
    """A light entity as Home Assistant shows it."""
    attributes: dict[str, Any] = {"supported_color_modes": list(modes), **attrs}
    if span is not None and "color_temp" in modes:
        attributes["min_color_temp_kelvin"], attributes["max_color_temp_kelvin"] = span
    if effects is not None:
        attributes["effect_list"] = list(effects)
        attributes["supported_features"] = 4
    else:
        attributes["supported_features"] = 0
    return EntityDesc(
        entity_id=f"light.gvh60a6abcd{'' if key == 'light' else '_' + key}",
        domain="light",
        role=role,
        key=key,
        enabled=enabled,
        state=state if enabled else None,
        attributes=attributes if enabled else {},
        zone=zone,
        segment=segment,
    )


def zone_light(name: str, **kwargs: Any) -> EntityDesc:
    """A zone light (no effects)."""
    kwargs.setdefault("effects", None)
    return light(f"zone_{name}_light", role=ROLE_ZONE_LIGHT, zone=name, **kwargs)


def segment(index: int, **kwargs: Any) -> EntityDesc:
    """A segment light (disabled by default, as the integration registers them)."""
    kwargs.setdefault("effects", None)
    kwargs.setdefault("enabled", False)
    return light(f"segment_{index}", role=ROLE_SEGMENT, segment=index, **kwargs)


def plug(state: str = "on") -> EntityDesc:
    """A relay plug's power switch."""
    return EntityDesc(
        entity_id="switch.ihoment_h5083_abce",
        domain="switch",
        role=ROLE_PLUG,
        key="power",
        enabled=True,
        state=state,
    )


def outlet(name: str, state: str = "on") -> EntityDesc:
    """One outlet of a per-outlet relay device (a zone switch on a device with no light)."""
    return EntityDesc(
        entity_id=f"switch.ihoment_h5086_abcf_zone_{name}",
        domain="switch",
        role=ROLE_ZONE_SWITCH,
        key=f"zone_{name}",
        enabled=True,
        state=state,
        zone=name,
    )


def self_test_button() -> EntityDesc:
    """The Run self-test button."""
    return EntityDesc(
        entity_id="button.gvh60a6abcd_self_test",
        domain="button",
        role=ROLE_SELF_TEST,
        key="self_test",
        enabled=True,
        state="unknown",
    )


def device(*entities: EntityDesc, **fields: Any) -> DeviceDesc:
    """A loaded device with ``entities``."""
    defaults: dict[str, Any] = {
        "label": "H60A6 #1",
        "sku": "H60A6",
        "entry_id": "entry-1",
        "title": TITLE,
        "names": (TITLE, USER_NAME),
        "address": ADDRESS,
        "area": AREA,
        "area_label": "area 1",
        "entry_state": "loaded",
        "capabilities": frozenset({"power", "brightness", "rgb", "color_temp", "scenes"}),
        "entities": entities,
        "source": LOCAL_ADAPTER,
        "source_name": LOCAL_ADAPTER_NAME,
        "source_label": "local adapter",
        "hex_ids": (ADDRESS, SERIAL),
    }
    defaults.update(fields)
    return DeviceDesc(**defaults)
