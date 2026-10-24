"""Tests for the config flow: discovery, user setup, secret collection, reauth, reconfigure."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
import govee_ble_local
from govee_ble_local import (
    Device,
    GoveeBleConnectionError,
    GoveeBleNotSupported,
    GoveeCloudAuthError,
)
from govee_ble_local.cloud import CloudDevice, GoveeCloudError
from govee_ble_local.identify import identify
from homeassistant.config_entries import (
    SOURCE_BLUETOOTH,
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResult, FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.govee_ble_local import config_flow
from custom_components.govee_ble_local.config_flow import GoveeBleLocalConfigFlow
from custom_components.govee_ble_local.const import (
    CONF_ALLOW_MODELLED,
    CONF_BROADCAST,
    CONF_CLEAR_SECRET,
    CONF_SECRET,
    CONF_SKU,
    DOMAIN,
)

from .conftest import ModelledCase, make_device, make_service_info
from .const import (
    ADDRESS,
    LOCAL_NAME,
    PLUG_ADDRESS,
    PLUG_NAME,
    SECRET_HEX,
    broadcast_data,
    pact_mfg,
)

FLOW = "custom_components.govee_ble_local.config_flow"
UNSUPPORTED_NAME = "GVH9999ABCD"
OTHER_SECRET_HEX = "0011223344556677"
PLUG_NO_COLON = PLUG_ADDRESS.replace(":", "").lower()
# Govee device ids are 8 octets ending in the Wi-Fi MAC, which is one below the BLE
# address the device advertises (the library's own example has the same pair).
PLUG_DEVICE_ID = "00:00:AA:BB:CC:DD:EE:06"
PLUG_WIFI_MAC = "AA:BB:CC:DD:EE:06"
OTHER_DEVICE_ID = "00:00:00:00:5E:00:53:20"
OTHER_BLE_MAC = "00:00:5E:00:53:21"


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
def cloud_account_cls() -> Generator[MagicMock]:
    """Patch the GoveeCloudAccount class (its instance is ``cloud_account``)."""
    with patch(f"{FLOW}.GoveeCloudAccount") as cls:
        cls.return_value.get_devices = AsyncMock(return_value=[])
        cls.return_value.close = AsyncMock()
        yield cls


@pytest.fixture
def cloud_account(cloud_account_cls: MagicMock) -> MagicMock:
    """The patched cloud account; tests set ``get_devices`` results."""
    account: MagicMock = cloud_account_cls.return_value
    return account


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
    address: str | None,
    device: str = PLUG_DEVICE_ID,
) -> CloudDevice:
    """Build a cloud account device the way the library's account parser fills one.

    ``address`` is the response's ``deviceSettings.address`` (the BLE MAC). Without it
    the parser takes the last 6 octets of the device id (the Wi-Fi MAC) as ``ble_mac``,
    and None only for an id with fewer than 6 octets.
    """
    octets = device.split(":")
    fallback = ":".join(octets[-6:]) if len(octets) >= 6 else None
    return CloudDevice(
        sku="H5083",
        device=device,
        name="Plug",
        secret=secret,
        pact_type=None,
        pact_code=None,
        goods_type=None,
        ble_mac=address or fallback,
    )


def other_cloud_device() -> CloudDevice:
    """Another device on the same account, with a secret of its own."""
    return cloud_device(
        secret=bytes.fromhex(OTHER_SECRET_HEX), address=OTHER_BLE_MAC, device=OTHER_DEVICE_ID
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


async def assert_set_up_once(hass: HomeAssistant, setup_entry: AsyncMock) -> None:
    """The created entry was set up, or the updated one reloaded, exactly once.

    For reauth/reconfigure the reload is what makes the new secret take effect.
    """
    await hass.async_block_till_done()
    assert len(setup_entry.mock_calls) == 1


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
    assert result["data"] == {
        CONF_ADDRESS: ADDRESS,
        CONF_SKU: "H60A6",
        CONF_BROADCAST: broadcast_data(),
    }
    assert result["result"].unique_id == ADDRESS
    await assert_set_up_once(hass, setup_entry)


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


_GOVEE_PAYLOAD = next(iter(make_service_info().manufacturer_data.values()))


@pytest.mark.parametrize(
    ("name", "company_id"),
    [
        ("Govee_B7080_1A2B", 0x8841),  # B7080: matched by Govee_*, admitted only by a later library
        ("GBK_B7082_1A2B", 0x8842),
        ("GVH50751A2B", 0x8801),  # a Govee thermometer: matched by GVH*, never a light
        (LOCAL_NAME, 0x8841),  # a supported light on the encrypted-header variants
        (LOCAL_NAME, 0x8842),
        ("GV60471A2B", 0x8843),  # plain GV + digit (GV6*): read as an H6047
    ],
)
async def test_bluetooth_discovery_follows_the_librarys_supported_check(
    hass: HomeAssistant, name: str, company_id: int
) -> None:
    """The matchers may catch more than the library drives; only what it supports is offered."""
    info = make_service_info(name=name, manufacturer_data={company_id: _GOVEE_PAYLOAD})
    adv = identify(info.name, info.manufacturer_data)
    supported = adv is not None and govee_ble_local.is_supported_sku(adv.sku, pact=adv.pact)
    if name in (LOCAL_NAME, "GV60471A2B"):
        assert supported  # supported lights on every library: these must reach the confirm step
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
    )
    if supported:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"
    else:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"


# H6009 is routed only by the library's catalogue: it is served at its row's pacts alone.
PACT_CASES = pytest.mark.parametrize(
    ("name", "pact", "offered"),
    [
        ("GVH6009ABCD", (1, 1), True),  # a pact its row accepts
        ("GVH6009ABCD", (2, 3), False),  # a pact it doesn't: setup would refuse the model
        (LOCAL_NAME, (9, 9), True),  # a 1.4.0 model still routes at a pact no row lists
    ],
    ids=["catalogue_accepted_pact", "catalogue_other_pact", "known_model_unlisted_pact"],
)


@PACT_CASES
async def test_bluetooth_discovery_checks_support_at_the_advertised_pact(
    hass: HomeAssistant, setup_entry: AsyncMock, name: str, pact: tuple[int, int], offered: bool
) -> None:
    """Discovery offers a model only where setup would route it: at its advertised pact."""
    info = make_service_info(name=name, manufacturer_data=pact_mfg(*pact))
    assert identify(info.name, info.manufacturer_data).pact == pact
    with patch(f"{FLOW}.profile_for", wraps=config_flow.profile_for) as profile:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
        )
        if not offered:
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "not_supported"
            return
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The secret check looked the profile up at the same pact (threaded, not re-derived).
    assert profile.call_args.kwargs == {"pact": pact, "allow_modelled": False}


@PACT_CASES
async def test_user_flow_offers_only_devices_supported_at_their_pact(
    hass: HomeAssistant, setup_entry: AsyncMock, name: str, pact: tuple[int, int], offered: bool
) -> None:
    """The manual picker lists a model only at a pact it is served at, and sets it up there."""
    infos = [make_service_info(name=name, manufacturer_data=pact_mfg(*pact))]
    with patch(f"{FLOW}.async_discovered_service_info", return_value=infos):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    if not offered:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "no_devices_found"
        return
    assert result["step_id"] == "user"
    assert next(iter(result["data_schema"].schema.values())).container == {ADDRESS: name}
    with patch(f"{FLOW}.profile_for", wraps=config_flow.profile_for) as profile:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert profile.call_args.kwargs == {"pact": pact, "allow_modelled": False}


# Another device's advertisement merged into the record: its name and its manufacturer block.
RENAMED = "5134P0098101970F"
FOREIGN = {11816: bytes.fromhex("89304dad0000")}


@pytest.mark.parametrize(
    ("name", "latest_name", "flow_mfg", "latest_mfg", "entry_pact"),
    [
        ("GVH6009ABCD", None, {}, pact_mfg(1, 1), (1, 1)),  # no pact, then one its row accepts
        ("GVH6009ABCD", None, {}, pact_mfg(2, 3), None),  # no pact, then one it doesn't: refused
        ("GVH6009ABCD", None, {}, {}, None),  # still no pact: any pact, as setup will take it
        ("GVH6009ABCD", None, {}, None, None),  # no connectable advert to check against
        ("GVH6009ABCD", None, pact_mfg(1, 1), pact_mfg(2, 3), None),  # the pact changed: refused
        (LOCAL_NAME, None, pact_mfg(1, 1), pact_mfg(9, 9), (9, 9)),  # changed, still served
        ("GVH6009ABCD", None, pact_mfg(1, 1), {}, (1, 1)),  # setup's advert has none
        ("GVH6009ABCD", None, pact_mfg(1, 1), None, (1, 1)),  # no connectable advert
        # Setup's advert shows another device's name: its Govee block still decides.
        ("GVH6009ABCD", RENAMED, pact_mfg(1, 1), {**FOREIGN, **pact_mfg(2, 3)}, None),
        (LOCAL_NAME, RENAMED, pact_mfg(1, 1), {**FOREIGN, **pact_mfg(9, 9)}, (9, 9)),
    ],
    ids=[
        "none_then_accepted",
        "none_then_refused",
        "still_none",
        "none_no_advert",
        "changed_refused",
        "changed_accepted",
        "pact_then_none",
        "pact_no_advert",
        "renamed_refused",
        "renamed_changed_accepted",
    ],
)
async def test_finish_rechecks_at_the_setup_advert_pact(
    hass: HomeAssistant,
    setup_entry: AsyncMock,
    *,
    name: str,
    latest_name: str | None,
    flow_mfg: dict[int, bytes],
    latest_mfg: dict[int, bytes] | None,
    entry_pact: tuple[int, int] | None,
) -> None:
    """The entry follows the pact setup will route by, and isn't created where it's refused."""
    info = make_service_info(name=name, manufacturer_data=flow_mfg)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
    )
    assert result["step_id"] == "bluetooth_confirm"
    latest = (
        None
        if latest_mfg is None
        else make_service_info(name=latest_name or name, manufacturer_data=latest_mfg)
    )
    with (
        patch(f"{FLOW}.async_last_service_info", return_value=latest) as last,
        patch(f"{FLOW}.profile_for", wraps=config_flow.profile_for) as profile,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert last.call_args.args[1:] == (ADDRESS,)
    assert last.call_args.kwargs == {"connectable": True}
    refused = latest_mfg not in (None, {}) and entry_pact is None
    if refused:
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "protocol_not_supported"
        assert hass.config_entries.async_entries(DOMAIN) == []
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert profile.call_args.kwargs == {"pact": entry_pact, "allow_modelled": False}
        # The entry keeps that advert's header, for a setup whose record has none.
        assert result["data"].get(CONF_BROADCAST) == (
            None if entry_pact is None else broadcast_data(*entry_pact)
        )


# -- modelled-only models ---------------------------------------------------------


async def test_modelled_model_asks_before_setup_and_records_the_opt_in(
    hass: HomeAssistant, setup_entry: AsyncMock, modelled: ModelledCase
) -> None:
    """Discovery asks before setting up a modelled-only model, and the entry records the yes."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=modelled.info()
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "modelled_confirm"
    assert result["menu_options"] == ["modelled_accept", "modelled_decline"]
    assert result["description_placeholders"] == {"name": modelled.name, "model": modelled.sku}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "modelled_accept"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADDRESS: ADDRESS,
        CONF_SKU: modelled.sku,
        CONF_ALLOW_MODELLED: True,
        CONF_BROADCAST: broadcast_data(*modelled.pact),
    }


async def test_declining_a_modelled_model_aborts(
    hass: HomeAssistant, modelled: ModelledCase
) -> None:
    """Declining the modelled-only model ends the flow without an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=modelled.info()
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "modelled_decline"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "modelled_declined"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_manual_setup_of_a_modelled_model_asks_too(
    hass: HomeAssistant, setup_entry: AsyncMock, modelled: ModelledCase
) -> None:
    """Picking a modelled-only model from the manual list asks the same question."""
    with patch(f"{FLOW}.async_discovered_service_info", return_value=[modelled.info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert next(iter(result["data_schema"].schema.values())).container == {ADDRESS: modelled.name}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "modelled_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "modelled_accept"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ALLOW_MODELLED] is True


async def test_modelled_opt_in_reaches_the_secret_check(
    hass: HomeAssistant, setup_entry: AsyncMock, modelled: ModelledCase
) -> None:
    """The profile behind the secret decision is the modelled one the user accepted."""
    with patch(f"{FLOW}.profile_for", wraps=config_flow.profile_for) as profile:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=modelled.info()
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "modelled_accept"}
        )
    assert profile.call_args.kwargs == {"pact": modelled.pact, "allow_modelled": True}


async def test_pactless_modelled_model_is_rechecked_with_the_opt_in(
    hass: HomeAssistant, setup_entry: AsyncMock, modelled: ModelledCase
) -> None:
    """A pact-less modelled-only model is re-checked at setup's pact as modelled, not refused."""
    info = modelled.info(manufacturer_data={})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
    )
    assert result["step_id"] == "modelled_confirm"
    with patch(f"{FLOW}.async_last_service_info", return_value=modelled.info()):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "modelled_accept"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ALLOW_MODELLED] is True


async def test_modelled_opt_in_reaches_the_secret_read(
    hass: HomeAssistant, setup_entry: AsyncMock, modelled: ModelledCase
) -> None:
    """A modelled device that needs a secret is read with the modelled profile.

    Pins both hand-offs: the read task passes the opt-in, and the read passes it to
    create_device.
    """
    ble_device = BLEDevice(address=ADDRESS, name=modelled.name, details={})
    with (
        patch(f"{FLOW}._requires_secret", return_value=True),
        patch(f"{FLOW}.async_ble_device_from_address", return_value=ble_device),
        patch(f"{FLOW}.async_last_service_info", return_value=modelled.info()),
        patch(f"{FLOW}.create_device", side_effect=GoveeBleConnectionError("no link")) as create,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=modelled.info()
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "modelled_accept"}
        )
        await hass.async_block_till_done()
    assert create.call_args.args[:2] == (ble_device, modelled.sku)
    assert create.call_args.kwargs == {"allow_modelled": True}


@pytest.mark.parametrize("opted_in", [True, False])
async def test_reconfigure_checks_the_secret_with_the_entrys_opt_in(
    hass: HomeAssistant, setup_entry: AsyncMock, modelled: ModelledCase, opted_in: bool
) -> None:
    """Reconfigure decides on the secret with the profile the entry was set up with."""
    data: dict[str, Any] = {CONF_ADDRESS: ADDRESS, CONF_SKU: modelled.sku}
    if opted_in:
        data[CONF_ALLOW_MODELLED] = True
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data=data)
    entry.add_to_hass(hass)
    with patch(f"{FLOW}.profile_for", wraps=config_flow.profile_for) as profile:
        result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    assert profile.call_args.args == (modelled.sku,)
    assert profile.call_args.kwargs == {"pact": None, "allow_modelled": opted_in}


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
    assert result["data"] == {
        CONF_ADDRESS: ADDRESS,
        CONF_SKU: "H60A6",
        CONF_BROADCAST: broadcast_data(),
    }
    # Setting the domain up also sets up the other (already configured) entry.
    await hass.async_block_till_done()
    set_up = [call.args[1].entry_id for call in setup_entry.call_args_list]
    assert set_up.count(result["result"].entry_id) == 1


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
        CONF_BROADCAST: broadcast_data(),
    }
    await assert_set_up_once(hass, setup_entry)


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
        CONF_BROADCAST: broadcast_data(),
    }
    plug_device.read_secret.assert_awaited_once()
    plug_device.stop.assert_awaited_once()
    await assert_set_up_once(hass, setup_entry)


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


async def test_closing_the_flow_mid_read_stops_the_link(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """Closing the dialog while the secret is being read still stops the device link.

    HA cancels the read task; without the stop the plug's connection slot would
    stay held until the library's idle disconnect.
    """
    gate_read(plug_device, returns=bytes.fromhex(SECRET_HEX))
    result = await start_plug_discovery(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    plug_device.read_secret.assert_awaited_once()
    plug_device.stop.assert_awaited_once()
    assert [f for f in hass.config_entries.flow.async_progress() if f["handler"] == DOMAIN] == []
    assert hass.config_entries.async_entries(DOMAIN) == []
    setup_entry.assert_not_called()


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
        CONF_BROADCAST: broadcast_data(),
    }
    await assert_set_up_once(hass, setup_entry)


async def test_manual_secret_blank_allowed_on_setup(
    hass: HomeAssistant, plug_device: Device, setup_entry: AsyncMock
) -> None:
    """On first setup a blank secret creates the entry without a secret key."""
    menu = await to_secret_menu(hass, plug_device)
    form = await choose(hass, menu, "secret_manual")
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {CONF_SECRET: ""})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == PLUG_NAME
    assert result["data"] == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_BROADCAST: broadcast_data(),
    }
    assert CONF_SECRET not in result["data"]
    await assert_set_up_once(hass, setup_entry)


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
    hass: HomeAssistant,
    plug_device: Device,
    cloud_account_cls: MagicMock,
    cloud_account: MagicMock,
    setup_entry: AsyncMock,
) -> None:
    """The secret of the device matching the address wins, not the first with a secret."""
    cloud_account.get_devices.return_value = [
        other_cloud_device(),
        cloud_device(secret=bytes.fromhex(SECRET_HEX), address=PLUG_ADDRESS),
    ]
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
        CONF_BROADCAST: broadcast_data(),
    }
    assert CONF_EMAIL not in result["data"]
    assert CONF_PASSWORD not in result["data"]
    # Logged in with the entered credentials, on Home Assistant's shared session.
    cloud_account_cls.assert_called_once_with(
        CREDENTIALS[CONF_EMAIL],
        CREDENTIALS[CONF_PASSWORD],
        session=async_get_clientsession(hass),
    )
    cloud_account.close.assert_awaited_once()
    await assert_set_up_once(hass, setup_entry)


async def test_cloud_secret_matches_lowercase_ble_mac(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock, setup_entry: AsyncMock
) -> None:
    """The BLE MAC match is case- and separator-insensitive."""
    cloud_account.get_devices.return_value = [
        cloud_device(secret=bytes.fromhex(SECRET_HEX), address=PLUG_ADDRESS.lower()),
    ]
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SECRET] == SECRET_HEX


async def test_cloud_secret_without_ble_address_is_not_matched_by_wifi_mac(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock
) -> None:
    """Without deviceSettings.address the fallback MAC is the Wi-Fi MAC, which doesn't match.

    The device id ends in the Wi-Fi MAC, one below the advertised BLE address, so the
    plug's own secret isn't picked: secret_not_found.
    """
    plug = cloud_device(secret=bytes.fromhex(SECRET_HEX), address=None)
    assert plug.ble_mac == PLUG_WIFI_MAC
    cloud_account.get_devices.return_value = [other_cloud_device(), plug]
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "secret_not_found"}
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


async def test_cloud_secret_rejected_credentials_show_invalid_auth(
    hass: HomeAssistant, plug_device: Device, cloud_account: MagicMock
) -> None:
    """Credentials the account rejects (GoveeCloudAuthError) show invalid_auth.

    Raised as the library's top-level export (its semver-stable API), a source independent
    of the cloud module the flow imports it from.
    """
    cloud_account.get_devices.side_effect = GoveeCloudAuthError("login rejected")
    form = await open_cloud_form(hass, plug_device)
    result = await hass.config_entries.flow.async_configure(form["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "secret_cloud"
    assert result["errors"] == {"base": "invalid_auth"}
    cloud_account.close.assert_awaited_once()


@pytest.mark.parametrize(
    "devices",
    [
        [],
        [other_cloud_device()],
        [cloud_device(secret=None, address=PLUG_ADDRESS)],
        [cloud_device(secret=b"", address=PLUG_ADDRESS)],
        [cloud_device(secret=bytes.fromhex(SECRET_HEX), address=None, device="AA:BB")],
        # Only the library's MAC is matched, never the raw id it couldn't split into octets.
        [cloud_device(secret=bytes.fromhex(SECRET_HEX), address=None, device=PLUG_NO_COLON)],
    ],
    ids=[
        "empty_account",
        "other_device_only",
        "match_without_secret",
        "match_empty_secret",
        "no_mac_at_all",
        "unsplit_device_id",
    ],
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
        cloud_device(secret=bytes.fromhex(SECRET_HEX), address=PLUG_ADDRESS)
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
    await assert_set_up_once(hass, setup_entry)


async def test_reauth_keeps_the_modelled_opt_in(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """Replacing the secret keeps the entry's opt-in: without it, setup would refuse the model."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX, CONF_ALLOW_MODELLED: True})
    menu = await start_reauth(hass, entry)
    form = await choose(hass, menu, "secret_manual")
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_SECRET: SECRET_HEX}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
        CONF_ALLOW_MODELLED: True,
    }


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
        other_cloud_device(),
        cloud_device(secret=bytes.fromhex(SECRET_HEX), address=PLUG_ADDRESS),
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
    await assert_set_up_once(hass, setup_entry)


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
    await assert_set_up_once(hass, setup_entry)


async def test_reconfigure_entry_without_sku(hass: HomeAssistant, setup_entry: AsyncMock) -> None:
    """An entry with no stored SKU is treated as not needing a secret."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["data_schema"].schema == {}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_ADDRESS: ADDRESS}
    await assert_set_up_once(hass, setup_entry)


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
    await assert_set_up_once(hass, setup_entry)


async def test_reconfigure_keeps_the_modelled_opt_in(
    hass: HomeAssistant, setup_entry: AsyncMock
) -> None:
    """Changing the secret keeps the entry's opt-in: without it, setup would refuse the model."""
    entry = plug_entry(**{CONF_SECRET: OTHER_SECRET_HEX, CONF_ALLOW_MODELLED: True})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECRET: SECRET_HEX}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_ADDRESS: PLUG_ADDRESS,
        CONF_SKU: "H5083",
        CONF_SECRET: SECRET_HEX,
        CONF_ALLOW_MODELLED: True,
    }


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
    await assert_set_up_once(hass, setup_entry)


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
    await assert_set_up_once(hass, setup_entry)


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
    await assert_set_up_once(hass, setup_entry)


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
    await assert_set_up_once(hass, setup_entry)


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
