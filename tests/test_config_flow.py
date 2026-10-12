"""Tests for the config flow: discovery, user setup, secret collection, reauth, reconfigure."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
from govee_ble_local import Device, GoveeBleConnectionError, GoveeBleNotSupported
from govee_ble_local.cloud import CloudDevice, GoveeCloudError
from homeassistant.config_entries import (
    SOURCE_BLUETOOTH,
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResult, FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local.config_flow import GoveeBleLocalConfigFlow
from custom_components.govee_ble_local.const import (
    CONF_CLEAR_SECRET,
    CONF_SECRET,
    CONF_SKU,
    DOMAIN,
)

from .conftest import make_device, make_service_info
from .const import ADDRESS, LOCAL_NAME, PLUG_ADDRESS, PLUG_NAME, SECRET_HEX

FLOW = "custom_components.govee_ble_local.config_flow"
UNSUPPORTED_NAME = "GVH9999ABCD"
OTHER_SECRET_HEX = "0011223344556677"
PLUG_NO_COLON = PLUG_ADDRESS.replace(":", "").lower()


@pytest.fixture
async def setup_entry(hass: HomeAssistant) -> AsyncGenerator[AsyncMock]:
    """Stop created/updated entries from really setting the integration up."""
    with (
        patch("custom_components.govee_ble_local.async_setup_entry", return_value=True) as mock,
        patch("custom_components.govee_ble_local.async_unload_entry", return_value=True),
    ):
        yield mock
        # Unload while the stub is still patched (hass teardown would use the real one).
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def plug_device() -> Generator[Device]:
    """Patch the flow's BLE plumbing so a real H5083 plug Device is "read"."""
    device = make_device("H5083", address=PLUG_ADDRESS, name=PLUG_NAME)
    ble_device = BLEDevice(address=PLUG_ADDRESS, name=PLUG_NAME, details={})
    with (
        patch(f"{FLOW}.async_ble_device_from_address", return_value=ble_device),
        patch(
            f"{FLOW}.async_last_service_info",
            return_value=make_service_info(address=PLUG_ADDRESS, name=PLUG_NAME),
        ),
        patch(f"{FLOW}.create_device", return_value=device),
    ):
        yield device


@pytest.fixture
def cloud_account() -> Generator[MagicMock]:
    """Patch GoveeCloudAccount; tests set ``get_devices`` results."""
    with patch(f"{FLOW}.GoveeCloudAccount") as cls:
        account = cls.return_value
        account.get_devices = AsyncMock(return_value=[])
        account.close = AsyncMock()
        yield account


def plug_entry(**extra: Any) -> MockConfigEntry:
    """A config entry for the secret-gated plug."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=PLUG_NAME,
        unique_id=PLUG_ADDRESS,
        data={CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083", **extra},
    )


def cloud_device(
    *,
    secret: bytes | None,
    ble_mac: str | None,
    device: str = "98:17:3C:B1:A2:D0:00:11:22:33:44:55",
) -> CloudDevice:
    """Build a cloud account device."""
    return CloudDevice(
        sku="H5083",
        device=device,
        name="Plug",
        secret=secret,
        pact_type=None,
        pact_code=None,
        goods_type=None,
        ble_mac=ble_mac,
    )


async def start_plug_discovery(hass: HomeAssistant) -> FlowResult:
    """Discover the plug over Bluetooth and confirm it (lands on the secret step)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=make_service_info(address=PLUG_ADDRESS, name=PLUG_NAME),
    )
    assert result["step_id"] == "bluetooth_confirm"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


def gate_read(
    device: Device, *, returns: bytes | None = None, raises: BaseException | None = None
) -> asyncio.Event:
    """Make ``device.read_secret`` wait for the returned event, then return/raise."""
    gate = asyncio.Event()

    async def _read() -> bytes | None:
        await gate.wait()
        if raises is not None:
            raise raises
        return returns

    device.read_secret.side_effect = _read
    return gate


async def finish_read(hass: HomeAssistant, result: FlowResult, gate: asyncio.Event) -> FlowResult:
    """Release the in-progress secret read and continue the flow."""
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "secret"
    assert result["progress_action"] == "read_secret"
    gate.set()
    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def run_plug_read(
    hass: HomeAssistant,
    device: Device,
    *,
    returns: bytes | None = None,
    raises: BaseException | None = None,
) -> FlowResult:
    """Discover the plug, let the device read finish with the given outcome, continue."""
    gate = gate_read(device, returns=returns, raises=raises)
    return await finish_read(hass, await start_plug_discovery(hass), gate)


async def to_secret_menu(hass: HomeAssistant, plug_device: Device) -> FlowResult:
    """Drive the plug flow to the secret menu (device read returns nothing)."""
    result = await run_plug_read(hass, plug_device)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "secret_menu"
    return result


async def choose(hass: HomeAssistant, result: FlowResult, option: str) -> FlowResult:
    """Pick a menu option."""
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


# -- Bluetooth discovery ------------------------------------------------------


async def test_bluetooth_discovery_creates_entry(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """A supported light is offered for confirmation and added with its SKU."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": LOCAL_NAME}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == LOCAL_NAME
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}
    assert result["result"].unique_id == ADDRESS


async def test_bluetooth_discovery_already_configured(hass: HomeAssistant) -> None:
    """Discovering an already-configured address aborts."""
    MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    "info",
    [
        make_service_info(name=UNSUPPORTED_NAME),
        make_service_info(name="NotAGoveeDevice"),
        make_service_info(name=""),
    ],
    ids=["unsupported_sku", "unknown_name", "nameless"],
)
async def test_bluetooth_discovery_not_supported(hass: HomeAssistant, info: Any) -> None:
    """Unsupported or unidentifiable devices abort with not_supported."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


# -- user flow ----------------------------------------------------------------


async def test_user_flow_lists_only_supported_new_devices(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """Only supported, nameless-free, not-yet-configured devices are offered."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:10",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:10", CONF_SKU: "H60A6"},
    ).add_to_hass(hass)
    infos = [
        make_service_info(),
        make_service_info(address="AA:BB:CC:DD:EE:10", name="GVH60A6CCCC"),  # configured
        make_service_info(address="AA:BB:CC:DD:EE:11", name=""),  # nameless
        make_service_info(address="AA:BB:CC:DD:EE:12", name=UNSUPPORTED_NAME),  # unsupported
        make_service_info(address="AA:BB:CC:DD:EE:13", name="Random Speaker"),  # not Govee
    ]
    with patch(f"{FLOW}.async_discovered_service_info", return_value=infos):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    options = next(iter(result["data_schema"].schema.values())).container
    assert options == {ADDRESS: LOCAL_NAME}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == LOCAL_NAME
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    """With nothing supported in range the flow aborts."""
    with patch(f"{FLOW}.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_all_devices_configured(hass: HomeAssistant) -> None:
    """If every discovered device is already configured there is nothing to pick."""
    MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}
    ).add_to_hass(hass)
    with patch(f"{FLOW}.async_discovered_service_info", return_value=[make_service_info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_configured_meanwhile(hass: HomeAssistant) -> None:
    """Picking a device configured after the form was shown aborts."""
    with patch(f"{FLOW}.async_discovered_service_info", return_value=[make_service_info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_secret_device_goes_to_secret_step(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """Picking a secret-gated device from the list collects its secret."""
    gate = gate_read(plug_device, returns=bytes.fromhex(SECRET_HEX))
    infos = [make_service_info(address=PLUG_ADDRESS, name=PLUG_NAME)]
    with patch(f"{FLOW}.async_discovered_service_info", return_value=infos):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: PLUG_ADDRESS}
    )
    result = await finish_read(hass, result, gate)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
    }


# -- secret read off the device ----------------------------------------------


async def test_secret_read_shows_progress_then_creates_entry(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """A secret read off an unbound plug is stored and the link is stopped."""
    gate = gate_read(plug_device, returns=bytes.fromhex(SECRET_HEX))
    result = await start_plug_discovery(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "secret"
    assert result["progress_action"] == "read_secret"

    result = await finish_read(hass, result, gate)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PLUG_NAME
    assert result["data"] == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
    }
    plug_device.read_secret.assert_awaited_once()
    plug_device.stop.assert_awaited_once()


async def test_secret_progress_not_done_keeps_showing_progress(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """Re-entering the secret step mid-read shows progress without a second read."""
    gate = gate_read(plug_device, returns=bytes.fromhex(SECRET_HEX))
    result = await start_plug_discovery(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    again = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert again["type"] is FlowResultType.SHOW_PROGRESS
    assert plug_device.read_secret.await_count == 1

    result = await finish_read(hass, again, gate)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_secret_read_passes_advertisement_to_create_device(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """The device is built from the connectable BLEDevice and last advertisement."""
    gate = gate_read(plug_device, returns=bytes.fromhex(SECRET_HEX))
    info = make_service_info(address=PLUG_ADDRESS, name=PLUG_NAME)
    with (
        patch(f"{FLOW}.async_last_service_info", return_value=info),
        patch(f"{FLOW}.create_device", return_value=plug_device) as create,
    ):
        await finish_read(hass, await start_plug_discovery(hass), gate)
    ble_device, sku, advertisement = create.call_args.args
    assert ble_device.address == PLUG_ADDRESS
    assert sku == "H5083"
    assert advertisement is info.advertisement


async def test_secret_read_without_last_service_info(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """A missing last advertisement doesn't stop the read."""
    gate = gate_read(plug_device, returns=bytes.fromhex(SECRET_HEX))
    with (
        patch(f"{FLOW}.async_last_service_info", return_value=None),
        patch(f"{FLOW}.create_device", return_value=plug_device) as create,
    ):
        result = await finish_read(hass, await start_plug_discovery(hass), gate)
    assert create.call_args.args[2] is None
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SECRET] == SECRET_HEX


@pytest.mark.parametrize(
    "outcome",
    [
        {"returns": None},
        {"returns": b""},
        {"raises": GoveeBleConnectionError("no connection")},
        {"raises": TimeoutError()},
    ],
    ids=["none", "empty", "connection_error", "timeout"],
)
async def test_secret_read_failure_offers_menu(
    hass: HomeAssistant, plug_device: Device, outcome: dict[str, Any]
) -> None:
    """If the device yields no secret, the cloud/manual menu is offered."""
    result = await run_plug_read(hass, plug_device, **outcome)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "secret_menu"
    assert result["menu_options"] == ["secret_cloud", "secret_manual"]
    plug_device.stop.assert_awaited_once()


async def test_secret_read_without_ble_device(hass: HomeAssistant) -> None:
    """No connectable BLEDevice in range falls straight through to the menu."""
    with (
        patch(f"{FLOW}.async_ble_device_from_address", return_value=None),
        patch(f"{FLOW}.create_device") as create,
    ):
        result = await start_plug_discovery(hass)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "secret_menu"
    create.assert_not_called()


async def test_secret_read_device_not_supported(hass: HomeAssistant) -> None:
    """A library NotSupported error while building the device falls to the menu."""
    with (
        patch(
            f"{FLOW}.async_ble_device_from_address",
            return_value=BLEDevice(address=PLUG_ADDRESS, name=PLUG_NAME, details={}),
        ),
        patch(f"{FLOW}.async_last_service_info", return_value=None),
        patch(f"{FLOW}.create_device", side_effect=GoveeBleNotSupported("nope")),
    ):
        result = await start_plug_discovery(hass)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "secret_menu"


# -- manual secret ------------------------------------------------------------


async def test_manual_secret_invalid(hass: HomeAssistant, plug_device: Device) -> None:
    """Non-hex and wrong-length secrets are rejected on the form."""
    menu = await to_secret_menu(hass, plug_device)
    form = await choose(hass, menu, "secret_manual")
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "secret_manual"
    for bad in ("not hex at all!", "a1b2c3d4e5f6"):
        form = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_SECRET: bad})
        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == "secret_manual"
        assert form["errors"] == {CONF_SECRET: "invalid_secret"}


@pytest.mark.parametrize(
    "raw",
    ["A1:B2:C3:D4:E5:F6:07:18", SECRET_HEX.upper(), " a1b2 c3d4 e5f6 0718 "],
)
async def test_manual_secret_normalized(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock, raw: str
) -> None:
    """Separators, case and spaces are normalized before storing."""
    menu = await to_secret_menu(hass, plug_device)
    form = await choose(hass, menu, "secret_manual")
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_SECRET: raw})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
    }


async def test_manual_secret_blank_allowed_on_setup(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """On first setup a blank secret creates the entry without a secret key."""
    menu = await to_secret_menu(hass, plug_device)
    form = await choose(hass, menu, "secret_manual")
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_SECRET: ""})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PLUG_NAME
    assert result["data"] == {CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083"}
    assert CONF_SECRET not in result["data"]


# -- cloud secret -------------------------------------------------------------


async def open_cloud_form(hass: HomeAssistant, plug_device: Device) -> FlowResult:
    """Reach the cloud credentials form of a new plug flow."""
    menu = await to_secret_menu(hass, plug_device)
    form = await choose(hass, menu, "secret_cloud")
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "secret_cloud"
    return form


CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"}


async def test_cloud_secret_picks_matching_device(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock, setup_entry: AsyncMock
) -> None:
    """The secret of the device matching the address wins, not the first with a secret."""
    cloud_account.get_devices.return_value = [
        cloud_device(secret=bytes.fromhex(OTHER_SECRET_HEX), ble_mac="98:17:3C:B1:A2:D1"),
        cloud_device(secret=bytes.fromhex(SECRET_HEX), ble_mac=PLUG_ADDRESS),
    ]
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
    }
    assert CONF_EMAIL not in result["data"]
    assert CONF_PASSWORD not in result["data"]
    cloud_account.close.assert_awaited_once()


async def test_cloud_secret_matches_lowercase_ble_mac(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock, setup_entry: AsyncMock
) -> None:
    """The BLE MAC match is case- and separator-insensitive."""
    cloud_account.get_devices.return_value = [
        cloud_device(secret=bytes.fromhex(SECRET_HEX), ble_mac=PLUG_ADDRESS.lower()),
    ]
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SECRET] == SECRET_HEX


async def test_cloud_secret_device_id_fallback(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock, setup_entry: AsyncMock
) -> None:
    """Without a BLE MAC, the device id's trailing octets identify the device."""
    cloud_account.get_devices.return_value = [
        cloud_device(
            secret=bytes.fromhex(OTHER_SECRET_HEX),
            ble_mac=None,
            device="98:17:3C:B1:A2:D0:11:22:33:44:55:66",
        ),
        cloud_device(
            secret=bytes.fromhex(SECRET_HEX),
            ble_mac=None,
            device=f"98:17:3C:B1:A2:D0:{PLUG_ADDRESS.lower()}",
        ),
    ]
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SECRET] == SECRET_HEX
    cloud_account.close.assert_awaited_once()


async def test_cloud_secret_login_error(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock
) -> None:
    """A cloud failure is shown as cannot_connect and the session is closed."""
    cloud_account.get_devices.side_effect = GoveeCloudError("login failed")
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "secret_cloud"
    assert result["errors"] == {"base": "cannot_connect"}
    cloud_account.close.assert_awaited_once()


@pytest.mark.parametrize(
    "devices",
    [
        [],
        [cloud_device(secret=bytes.fromhex(OTHER_SECRET_HEX), ble_mac="98:17:3C:B1:A2:D1")],
        [cloud_device(secret=None, ble_mac=PLUG_ADDRESS)],
        [cloud_device(secret=b"", ble_mac=PLUG_ADDRESS)],
    ],
    ids=["empty_account", "other_device_only", "match_without_secret", "match_empty_secret"],
)
async def test_cloud_secret_not_found(
    hass: HomeAssistant,
    plug_device: Device,
    cloud_account: MagicMock,
    devices: list[CloudDevice],
) -> None:
    """No matching device with a key reports secret_not_found."""
    cloud_account.get_devices.return_value = devices
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "secret_not_found"}
    cloud_account.close.assert_awaited_once()


async def test_cloud_secret_retry_after_error(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock, setup_entry: AsyncMock
) -> None:
    """After an error the form can be resubmitted successfully."""
    cloud_account.get_devices.side_effect = GoveeCloudError("boom")
    form = await open_cloud_form(hass, plug_device)
    form = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert form["errors"] == {"base": "cannot_connect"}

    cloud_account.get_devices.side_effect = None
    cloud_account.get_devices.return_value = [
        cloud_device(secret=bytes.fromhex(SECRET_HEX), ble_mac=PLUG_ADDRESS)
    ]
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SECRET] == SECRET_HEX


# -- unreachable-state guards -------------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        "async_step_bluetooth_confirm",
        "async_step_secret",
        "async_step_secret_menu",
        "async_step_secret_manual",
        "async_step_secret_cloud",
        "async_step_reauth_confirm",
        "async_step_secret_read",
    ],
)
async def test_steps_abort_without_context(hass: HomeAssistant, step: str) -> None:
    """Steps reached without discovery/pending state abort rather than crash."""
    flow = GoveeBleLocalConfigFlow()
    flow.hass = hass
    with pytest.raises(AbortFlow) as err:
        await getattr(flow, step)()
    assert err.value.reason == "unknown"


# -- reauth -------------------------------------------------------------------


async def start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> FlowResult:
    """Start the reauth flow for an entry."""
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"
    assert result["menu_options"] == ["secret_cloud", "secret_manual"]
    return result


async def test_reauth_manual_updates_secret(hass: HomeAssistant, setup_entry: AsyncMock) -> None:
    """A valid manual key replaces the stored one and preserves other data."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    menu = await start_reauth(hass, entry)
    form = await choose(hass, menu, "secret_manual")
    assert form["step_id"] == "secret_manual"

    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_SECRET: "A1:B2:C3:D4:E5:F6:07:18"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083", CONF_SECRET: SECRET_HEX}


async def test_reauth_manual_blank_rejected(hass: HomeAssistant) -> None:
    """A blank key can't replace a rejected one."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    menu = await start_reauth(hass, entry)
    form = await choose(hass, menu, "secret_manual")
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_SECRET: ""})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SECRET: "invalid_secret"}
    assert entry.data[CONF_SECRET] == OTHER_SECRET_HEX


async def test_reauth_manual_invalid_rejected(hass: HomeAssistant) -> None:
    """A malformed key is rejected during reauth too."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    menu = await start_reauth(hass, entry)
    form = await choose(hass, menu, "secret_manual")
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_SECRET: "xyz"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_SECRET: "invalid_secret"}
    assert entry.data[CONF_SECRET] == OTHER_SECRET_HEX


async def test_reauth_cloud_updates_secret(
    hass: HomeAssistant, cloud_account: MagicMock, setup_entry: AsyncMock
) -> None:
    """The cloud path replaces the stored secret during reauth."""
    cloud_account.get_devices.return_value = [
        cloud_device(secret=bytes.fromhex(OTHER_SECRET_HEX), ble_mac="98:17:3C:B1:A2:D1"),
        cloud_device(secret=bytes.fromhex(SECRET_HEX), ble_mac=PLUG_ADDRESS),
    ]
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    menu = await start_reauth(hass, entry)
    form = await choose(hass, menu, "secret_cloud")
    assert form["step_id"] == "secret_cloud"

    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083", CONF_SECRET: SECRET_HEX}
    cloud_account.close.assert_awaited_once()


async def test_reauth_via_flow_init(hass: HomeAssistant) -> None:
    """The reauth source reached through flow.async_init also shows the menu."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_confirm"


# -- reconfigure --------------------------------------------------------------


async def test_reconfigure_device_without_secret(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """A device that needs no secret gets an empty form and keeps its data."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["data_schema"].schema == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_ADDRESS: ADDRESS, CONF_SKU: "H60A6"}


async def test_reconfigure_entry_without_sku(hass: HomeAssistant, setup_entry: AsyncMock) -> None:
    """An entry with no stored SKU is treated as not needing a secret."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["data_schema"].schema == {}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_ADDRESS: ADDRESS}


async def test_reconfigure_secret_form_fields(hass: HomeAssistant) -> None:
    """A secret-gated device is offered the secret and clear fields."""
    entry = plug_entry(**{CONF_SECRET: SECRET_HEX})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert {str(key) for key in result["data_schema"].schema} == {CONF_SECRET, CONF_CLEAR_SECRET}


@pytest.mark.parametrize("user_input", [{}, {CONF_SECRET: ""}, {CONF_SECRET: "  "}])
async def test_reconfigure_blank_keeps_secret(
    hass: HomeAssistant, setup_entry: AsyncMock, user_input: dict[str, Any]
) -> None:
    """A blank secret field keeps the stored secret (it must not be wiped)."""
    entry = plug_entry(**{CONF_SECRET: SECRET_HEX})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == SECRET_HEX


async def test_reconfigure_replaces_secret(hass: HomeAssistant, setup_entry: AsyncMock) -> None:
    """A new valid secret replaces the stored one."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: "A1:B2:C3:D4:E5:F6:07:18"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083", CONF_SECRET: SECRET_HEX}


async def test_reconfigure_sets_secret_when_missing(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """A plug set up without a secret can be given one later."""
    entry = plug_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: SECRET_HEX}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == SECRET_HEX


async def test_reconfigure_clear_secret(hass: HomeAssistant, setup_entry: AsyncMock) -> None:
    """The clear checkbox removes the stored secret."""
    entry = plug_entry(**{CONF_SECRET: SECRET_HEX})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: "", CONF_CLEAR_SECRET: True}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083"}


async def test_reconfigure_new_secret_wins_over_clear(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """Entering a new secret with clear ticked still stores the new secret."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: SECRET_HEX, CONF_CLEAR_SECRET: True}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SECRET] == SECRET_HEX


async def test_reconfigure_invalid_secret(hass: HomeAssistant) -> None:
    """An invalid secret shows a form error and leaves the data untouched."""
    entry = plug_entry(**{CONF_SECRET: SECRET_HEX})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: "a1b2c3", CONF_CLEAR_SECRET: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {CONF_SECRET: "invalid_secret"}
    assert entry.data == {CONF_ADDRESS: PLUG_ADDRESS, CONF_SKU: "H5083", CONF_SECRET: SECRET_HEX}
