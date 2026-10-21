"""Config flow for the Govee BLE Local integration.

Home Assistant discovers Govee devices by manufacturer id and local-name
prefix (see manifest); the library's advertisement identifier + SKU registry
then decide whether the specific model is supported.

Some devices (the smart-plug family) gate command processing behind an 8-byte
secret key. For those, the flow first tries to read the key off the device
(works only while it is unbound), then offers fetching it from the Govee cloud
account (``secretCode``) or entering it manually (e.g. from a btsnoop capture).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
from typing import Any

from govee_ble_local import GoveeBleError, create_device, is_supported_sku, profile_for
from govee_ble_local.cloud import GoveeCloudAccount, GoveeCloudAuthError, GoveeCloudError
from govee_ble_local.identify import identify
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio as vol

from .const import CONF_ALLOW_MODELLED, CONF_CLEAR_SECRET, CONF_SECRET, CONF_SKU, DOMAIN
from .helpers import BLE_ERRORS, advertised_pact, parse_secret

_LOGGER = logging.getLogger(__name__)

_MENU_OPTIONS = ["secret_cloud", "secret_manual"]
_MODELLED_OPTIONS = ["modelled_accept", "modelled_decline"]
_SECRET_SCHEMA = vol.Schema({vol.Optional(CONF_SECRET, default=""): str})
_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


@dataclass(frozen=True, kw_only=True)
class _PendingDevice:
    """The device being set up, or whose secret is being replaced (reauth)."""

    address: str
    sku: str
    title: str
    # The advertised (pact_type, pact_code): the library serves some models only at their
    # row's pacts, so support and the profile are looked up at it. None (an advert with no
    # parseable pact, or reauth) looks the SKU up at any pact. _finish() re-checks the
    # device at the pact of the advert setup will route by, when that one differs.
    pact: tuple[int, int] | None = None
    # Supported only as "modelled" (allow_modelled): set up only after the user confirms.
    modelled: bool = False


class _CloudSecretError(Exception):
    """The cloud lookup failed; ``reason`` is the flow error key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _supported_device(info: BluetoothServiceInfoBleak) -> _PendingDevice | None:
    """Return the device to set up if this advertisement is a supported Govee device.

    Support is checked at the advertised pact, as setup's create_device() routes by it:
    a model the library serves only at some pacts is not offered at another one. A model
    the library supports only as "modelled" is returned flagged, for the user to confirm.
    """
    adv = identify(info.name, info.manufacturer_data)
    if adv is None:
        return None
    if is_supported_sku(adv.sku, pact=adv.pact):
        modelled = False
    elif is_supported_sku(adv.sku, pact=adv.pact, allow_modelled=True):
        modelled = True
    else:
        return None
    return _PendingDevice(
        address=info.address, sku=adv.sku, title=info.name, pact=adv.pact, modelled=modelled
    )


def _requires_secret(
    sku: str, pact: tuple[int, int] | None = None, *, allow_modelled: bool = False
) -> bool:
    """Return True if this SKU's device profile (at ``pact``) gates commands behind a secret."""
    profile = profile_for(sku, pact=pact, allow_modelled=allow_modelled)
    return bool(profile and profile.requires_secret)


class GoveeBleLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee BLE devices."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._pending: _PendingDevice | None = None
        self._discovered: dict[str, _PendingDevice] = {}  # keyed by address
        self._read_task: asyncio.Task[str | None] | None = None
        self._device_secret: str | None = None

    def _require_pending(self) -> _PendingDevice:
        if self._pending is None:
            raise AbortFlow("unknown")
        return self._pending

    # -- discovery ----------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a Govee device discovered over Bluetooth."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        pending = _supported_device(discovery_info)
        if pending is None:
            return self.async_abort(reason="not_supported")
        self._pending = pending
        self.context["title_placeholders"] = {"name": discovery_info.name}
        if pending.modelled:
            return await self.async_step_modelled_confirm()
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of a Bluetooth-discovered device."""
        pending = self._require_pending()
        if user_input is not None:
            return await self._finish(pending)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": pending.title},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle manual setup by picking from supported discovered devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            device = self._discovered[address]
            if device.modelled:
                self._pending = device
                return await self.async_step_modelled_confirm()
            return await self._finish(device)

        current = self._async_current_ids()
        self._discovered = {}
        for info in async_discovered_service_info(self.hass):
            if info.address in current or not info.name:
                continue
            if (found := _supported_device(info)) is not None:
                self._discovered[info.address] = found
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {address: device.title for address, device in self._discovered.items()}
                    )
                }
            ),
        )

    async def async_step_modelled_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before setting up a model whose support is modelled, not traced or verified."""
        pending = self._require_pending()
        return self.async_show_menu(
            step_id="modelled_confirm",
            menu_options=_MODELLED_OPTIONS,
            description_placeholders={"name": pending.title, "model": pending.sku},
        )

    async def async_step_modelled_accept(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the modelled device up after all (its entry records the choice)."""
        return await self._finish(self._require_pending())

    async def async_step_modelled_decline(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Don't set the modelled device up."""
        return self.async_abort(reason="modelled_declined")

    async def _finish(self, device: _PendingDevice) -> ConfigFlowResult:
        """Create the entry, or first collect the secret key if the device needs one."""
        if (pact := self._setup_pact(device.address)) is not None and pact != device.pact:
            # Setup routes by the latest connectable advert, whose pact can differ from the
            # flow's (which may have had none: any pact), so don't create an entry it would
            # refuse. Without one, setup waits for the device and the flow's pact stands.
            if not is_supported_sku(device.sku, pact=pact, allow_modelled=device.modelled):
                return self.async_abort(reason="protocol_not_supported")
            device = replace(device, pact=pact)
        if _requires_secret(device.sku, device.pact, allow_modelled=device.modelled):
            self._pending = device
            return await self.async_step_secret()
        return self._create_entry(device)

    def _setup_pact(self, address: str) -> tuple[int, int] | None:
        """Return the pact of the advert setup will route by (the latest connectable one)."""
        info = async_last_service_info(self.hass, address, connectable=True)
        return advertised_pact(info.name, info.manufacturer_data) if info else None

    def _create_entry(self, device: _PendingDevice, secret: str | None = None) -> ConfigFlowResult:
        """Create the entry for ``device``, with its secret key if it has one."""
        data: dict[str, Any] = {CONF_ADDRESS: device.address, CONF_SKU: device.sku}
        if secret:
            data[CONF_SECRET] = secret
        if device.modelled:
            data[CONF_ALLOW_MODELLED] = True
        return self.async_create_entry(title=device.title, data=data)

    # -- secret -------------------------------------------------------------

    async def _read_secret_from_device(
        self, address: str, sku: str, *, allow_modelled: bool = False
    ) -> str | None:
        """Try to read the 8-byte secret straight off the device over BLE.

        Works only while the device is UNBOUND (factory-reset, not paired to the
        Govee app); a bound device declines. Returns the secret as hex, or None
        if it couldn't be read.
        """
        ble_device = async_ble_device_from_address(self.hass, address, connectable=True)
        if ble_device is None:
            return None
        info = async_last_service_info(self.hass, address, connectable=True)
        try:
            device = create_device(
                ble_device,
                sku,
                info.advertisement if info else None,
                allow_modelled=allow_modelled,
            )
        except GoveeBleError as err:
            _LOGGER.debug("Cannot create device %s (%s): %s", address, sku, err)
            return None
        try:
            raw = await device.read_secret()
        except BLE_ERRORS as err:
            _LOGGER.debug("read_secret for %s failed: %s", address, err)
            return None
        finally:
            await device.stop()
        return raw.hex() if raw else None

    def _save_secret(self, secret: str) -> ConfigFlowResult:
        """Finish with a secret: replace the rejected key on reauth, else create the entry."""
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates={CONF_SECRET: secret}
            )
        return self._create_entry(self._require_pending(), secret)

    async def async_step_secret(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Try to read the secret directly from the device (shown as progress)."""
        pending = self._require_pending()
        if self._read_task is None:
            self._read_task = self.hass.async_create_task(
                self._read_secret_from_device(
                    pending.address, pending.sku, allow_modelled=pending.modelled
                ),
                f"{DOMAIN}_read_secret_{pending.address}",
            )
        if not self._read_task.done():
            return self.async_show_progress(
                step_id="secret",
                progress_action="read_secret",
                progress_task=self._read_task,
                description_placeholders={"name": pending.title},
            )
        self._device_secret = self._read_task.result()
        self._read_task = None
        if self._device_secret is not None:
            return self.async_show_progress_done(next_step_id="secret_read")
        return self.async_show_progress_done(next_step_id="secret_menu")

    async def async_step_secret_read(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry with the secret read off the (unbound) device."""
        if self._device_secret is None:
            raise AbortFlow("unknown")
        return self._save_secret(self._device_secret)

    async def async_step_secret_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the cloud or manual way of providing the secret."""
        pending = self._require_pending()
        return self.async_show_menu(
            step_id="secret_menu",
            menu_options=_MENU_OPTIONS,
            description_placeholders={"name": pending.title},
        )

    async def async_step_secret_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the secret key as hex (or, on first setup, blank to set it later)."""
        pending = self._require_pending()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                secret = parse_secret(user_input.get(CONF_SECRET, ""))
            except ValueError:
                errors[CONF_SECRET] = "invalid_secret"
            else:
                if secret is not None:
                    return self._save_secret(secret)
                if self.source != SOURCE_REAUTH:
                    # First setup: blank means "add the key later" (reconfigure).
                    return self._create_entry(pending)
                # A blank key can't fix a rejected one.
                errors[CONF_SECRET] = "invalid_secret"
        return self.async_show_form(
            step_id="secret_manual",
            data_schema=_SECRET_SCHEMA,
            errors=errors,
            description_placeholders={"name": pending.title},
        )

    async def async_step_secret_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log into the Govee account and fetch this device's secret key.

        Credentials are used once to obtain the secret and are NOT stored - only
        the resulting key is saved in the config entry.
        """
        pending = self._require_pending()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                secret = await self._fetch_cloud_secret(
                    pending.address, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except _CloudSecretError as err:
                errors["base"] = err.reason
            else:
                return self._save_secret(secret)
        return self.async_show_form(
            step_id="secret_cloud",
            data_schema=_CLOUD_SCHEMA,
            errors=errors,
            description_placeholders={"name": pending.title},
        )

    async def _fetch_cloud_secret(self, address: str, email: str, password: str) -> str:
        """Return the secret (hex) of the account device matching ``address``.

        Raises _CloudSecretError("invalid_auth") when the account rejects the
        credentials (the library's GoveeCloudAuthError, for HTTP 401/403),
        _CloudSecretError("cannot_connect") when login/listing otherwise fails and
        _CloudSecretError("secret_not_found") when no matching device has a key.
        """
        want = address.replace(":", "").lower()
        account = GoveeCloudAccount(email, password, session=async_get_clientsession(self.hass))
        try:
            devices = await account.get_devices()
        except GoveeCloudAuthError as err:
            _LOGGER.debug("Govee cloud rejected the credentials: %s", err)
            raise _CloudSecretError("invalid_auth") from err
        except GoveeCloudError as err:
            _LOGGER.debug("Govee cloud login/list failed: %s", err)
            raise _CloudSecretError("cannot_connect") from err
        finally:
            await account.close()
        for dev in devices:
            # The library's best-effort BLE MAC: deviceSettings.address, else the last 6
            # octets of the device id, which can be the Wi-Fi MAC (one off the advertised
            # BLE address, so no match). None only for an id with fewer than 6 octets.
            mac = (dev.ble_mac or "").replace(":", "").lower()
            if mac.endswith(want) and dev.secret:
                return dev.secret.hex()
        raise _CloudSecretError("secret_not_found")

    # -- reauth -------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Collect a new secret after the device rejected the stored one.

        The key is wrong, or the device was re-paired to an account (which
        rotates it).
        """
        entry = self._get_reauth_entry()
        self._pending = _PendingDevice(
            address=entry_data[CONF_ADDRESS],
            sku=entry_data.get(CONF_SKU, ""),
            title=entry.title,
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the cloud or manual way of providing the new secret."""
        pending = self._require_pending()
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=_MENU_OPTIONS,
            description_placeholders={"name": pending.title},
        )

    # -- reconfigure --------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reload the entry and, for secret-gated devices, set/replace/clear the key.

        A blank secret keeps the stored key; only the explicit "clear" checkbox
        removes it. The entry is keyed by the immutable BLE address, which is
        never edited here.
        """
        entry = self._get_reconfigure_entry()
        sku: str = entry.data.get(CONF_SKU, "")
        needs_secret = bool(sku) and _requires_secret(
            sku, allow_modelled=bool(entry.data.get(CONF_ALLOW_MODELLED, False))
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            # Updating the stored secret doesn't need the device in range; the
            # reload picks it up whenever the device is next reachable.
            data = dict(entry.data)
            if needs_secret:
                try:
                    secret = parse_secret(user_input.get(CONF_SECRET, ""))
                except ValueError:
                    errors[CONF_SECRET] = "invalid_secret"
                else:
                    if secret:
                        data[CONF_SECRET] = secret
                    elif user_input.get(CONF_CLEAR_SECRET):
                        data.pop(CONF_SECRET, None)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)

        schema = (
            vol.Schema(
                {
                    vol.Optional(CONF_SECRET, default=""): str,
                    vol.Optional(CONF_CLEAR_SECRET, default=False): bool,
                }
            )
            if needs_secret
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": entry.title},
        )
