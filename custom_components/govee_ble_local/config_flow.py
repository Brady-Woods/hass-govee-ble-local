"""Config flow for the Govee BLE Local integration.

Home Assistant discovers any Govee device by manufacturer id (see manifest);
the library's advertisement identifier + SKU registry then decide whether the
specific model is supported.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from govee_ble_local import is_supported_sku
from govee_ble_local.identify import identify
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


def _supported_sku(info: BluetoothServiceInfoBleak) -> str | None:
    """Return the SKU if this advertisement is a supported Govee device."""
    adv = identify(info.name, info.manufacturer_data)
    if adv is not None and is_supported_sku(adv.sku):
        return adv.sku
    return None


class GoveeBleLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee BLE devices."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._sku: str | None = None
        self._discovered_devices: dict[str, str] = {}
        self._discovered_skus: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Govee device discovered over Bluetooth (by manufacturer id)."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        # The manufacturer-id matcher catches every Govee device; the SKU
        # registry gates which models this integration actually supports.
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
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery_info.name,
                data={"address": self._discovery_info.address, "sku": self._sku},
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
            return self.async_create_entry(
                title=self._discovered_devices[address],
                data={"address": address, "sku": self._discovered_skus.get(address)},
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-verify an existing entry's device is currently reachable.

        No user-editable parameters (the entry is keyed by the immutable BLE
        address); this just confirms the device is visible over Bluetooth right
        now and reloads the entry.
        """
        entry = self._get_reconfigure_entry()
        address: str = entry.data["address"]

        if user_input is not None:
            if async_ble_device_from_address(self.hass, address, connectable=True) is None:
                return self.async_abort(reason="not_found")
            return self.async_update_reload_and_abort(entry, data=entry.data)

        return self.async_show_form(
            step_id="reconfigure",
            description_placeholders={"name": entry.title},
        )
