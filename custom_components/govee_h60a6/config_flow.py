"""Config flow for the Govee H60A6 integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DEVICE_NAME_PREFIX, DOMAIN


class GoveeH60A6ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee H60A6."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a device discovered over Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
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
                data={"address": self._discovery_info.address},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovery_info.name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by picking from discovered devices."""
        if user_input is not None:
            address = user_input["address"]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_devices[address], data={"address": address}
            )

        current_addresses = self._async_current_ids()
        for discovery in async_discovered_service_info(self.hass):
            if discovery.address in current_addresses:
                continue
            if discovery.name and discovery.name.startswith(DEVICE_NAME_PREFIX):
                self._discovered_devices[discovery.address] = discovery.name

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

        There are no user-editable parameters (the entry is keyed entirely
        by the immutable BLE address), so this doesn't change any stored
        data. What it usefully does is confirm the configured light is
        actually visible to a Bluetooth adapter right now and, on success,
        reload the entry - a quick way to recover a device that dropped off
        without deleting and re-adding it.
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
