"""Config flow for the Govee BLE Local integration.

Home Assistant discovers any Govee device by manufacturer id (see manifest);
the library's advertisement identifier + SKU registry then decide whether the
specific model is supported.

Some devices (the smart-plug family) gate command processing behind an 8-byte
secret key. For those, the flow collects the secret (hex). The secret is a
stable, device-stored value; obtain it from the Govee cloud account
(``secretCode``), a btsnoop capture, or an unbound device's ``aa b1`` read.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from govee_ble_local import device_class_for_sku, is_supported_sku
from govee_ble_local.identify import identify
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_SECRET, DOMAIN


def _supported_sku(info: BluetoothServiceInfoBleak) -> str | None:
    """Return the SKU if this advertisement is a supported Govee device."""
    adv = identify(info.name, info.manufacturer_data)
    if adv is not None and is_supported_sku(adv.sku):
        return adv.sku
    return None


def _requires_secret(sku: str) -> bool:
    """True if this SKU's device class gates commands behind a secret key."""
    cls = device_class_for_sku(sku)
    return bool(cls and cls.requires_secret)


def _parse_secret(raw: str) -> str | None:
    """Normalize a user-entered secret to 16 lowercase hex chars, or raise
    ValueError. Empty input returns None (allowed: device is added, but
    commands will fail until a secret is set)."""
    cleaned = raw.strip().replace(":", "").replace(" ", "").lower()
    if not cleaned:
        return None
    bytes.fromhex(cleaned)  # raises ValueError on non-hex
    if len(cleaned) != 16:
        raise ValueError("secret must be 8 bytes (16 hex chars)")
    return cleaned


class GoveeBleLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee BLE devices."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._sku: str | None = None
        self._pending: dict[str, str] | None = None  # {address, sku, title}
        self._discovered_devices: dict[str, str] = {}
        self._discovered_skus: dict[str, str] = {}

    # -- discovery ----------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Govee device discovered over Bluetooth (by manufacturer id)."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        sku = _supported_sku(discovery_info)
        if sku is None:
            return self.async_abort(reason="not_supported")
        self._discovery_info = discovery_info
        self._sku = sku
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of a Bluetooth-discovered device."""
        assert self._discovery_info is not None and self._sku is not None
        if user_input is not None:
            return self._finish(
                self._discovery_info.address, self._sku, self._discovery_info.name
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovery_info.name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by picking from supported discovered devices."""
        if user_input is not None:
            address = user_input["address"]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._finish(
                address,
                self._discovered_skus.get(address, ""),
                self._discovered_devices[address],
            )

        current = self._async_current_ids()
        names: dict[str, str] = {}
        skus: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass):
            if info.address in current or not info.name:
                continue
            sku = _supported_sku(info)
            if sku is not None:
                names[info.address] = info.name
                skus[info.address] = sku
        self._discovered_devices, self._discovered_skus = names, skus
        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("address"): vol.In(self._discovered_devices)}
            ),
        )

    # -- secret -------------------------------------------------------------

    def _finish(self, address: str, sku: str, title: str) -> ConfigFlowResult:
        """Create the entry, or divert to the secret step first if the device
        gates commands behind a secret key."""
        if sku and _requires_secret(sku):
            self._pending = {"address": address, "sku": sku, "title": title}
            return self.async_show_form(
                step_id="secret",
                data_schema=vol.Schema({vol.Optional(CONF_SECRET, default=""): str}),
                description_placeholders={"name": title},
            )
        return self.async_create_entry(title=title, data={"address": address, "sku": sku})

    async def async_step_secret(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the device's 8-byte secret key (hex)."""
        assert self._pending is not None
        if user_input is not None:
            try:
                secret = _parse_secret(user_input.get(CONF_SECRET, ""))
            except ValueError:
                return self.async_show_form(
                    step_id="secret",
                    data_schema=vol.Schema(
                        {vol.Optional(CONF_SECRET, default=""): str}
                    ),
                    errors={CONF_SECRET: "invalid_secret"},
                    description_placeholders={"name": self._pending["title"]},
                )
            data = {"address": self._pending["address"], "sku": self._pending["sku"]}
            if secret:
                data[CONF_SECRET] = secret
            return self.async_create_entry(title=self._pending["title"], data=data)

        return self.async_show_form(
            step_id="secret",
            data_schema=vol.Schema({vol.Optional(CONF_SECRET, default=""): str}),
            description_placeholders={"name": self._pending["title"]},
        )

    # -- reconfigure --------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-verify reachability and, for secret-gated devices, let the user
        set/update the secret key. The entry is keyed by the immutable BLE
        address, which is never edited here."""
        entry = self._get_reconfigure_entry()
        address: str = entry.data["address"]
        sku: str = entry.data.get("sku", "")
        needs_secret = _requires_secret(sku) if sku else False

        if user_input is not None:
            if async_ble_device_from_address(self.hass, address, connectable=True) is None:
                return self.async_abort(reason="not_found")
            data = dict(entry.data)
            if needs_secret:
                try:
                    secret = _parse_secret(user_input.get(CONF_SECRET, ""))
                except ValueError:
                    return self._reconfigure_form(entry.title, needs_secret, error=True)
                if secret:
                    data[CONF_SECRET] = secret
                else:
                    data.pop(CONF_SECRET, None)
            return self.async_update_reload_and_abort(entry, data=data)

        return self._reconfigure_form(entry.title, needs_secret)

    def _reconfigure_form(
        self, title: str, needs_secret: bool, *, error: bool = False
    ) -> ConfigFlowResult:
        schema = (
            vol.Schema({vol.Optional(CONF_SECRET, default=""): str})
            if needs_secret
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors={CONF_SECRET: "invalid_secret"} if error else None,
            description_placeholders={"name": title},
        )
