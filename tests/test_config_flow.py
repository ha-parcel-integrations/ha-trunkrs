"""Tests for the Trunkrs config and options flows."""
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.config_flow import (
    normalize_postcode,
    normalize_trunkrs_nr,
    valid_postcode,
    valid_trunkrs_nr,
)
from custom_components.trunkrs.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    CONF_TRUNKRS_NR,
    DOMAIN,
)

_VERIFY = "custom_components.trunkrs.config_flow.async_verify_parcel"


# --- helpers ---------------------------------------------------------------


def test_normalizers():
    assert normalize_postcode(" 1234 ab ") == "1234AB"
    assert normalize_trunkrs_nr("  tr123  ") == "TR123"


def test_postcode_validation():
    assert valid_postcode("1234AB")
    assert not valid_postcode("1234")
    assert not valid_postcode("ABCDEF")


def test_trunkrs_nr_validation():
    assert valid_trunkrs_nr("TR123456")
    assert not valid_trunkrs_nr("AB")          # too short
    assert not valid_trunkrs_nr("has space")   # invalid character


# --- config flow -----------------------------------------------------------


async def test_create_hub(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234 ab"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Trunkrs (1234AB)"
    assert result["options"][CONF_POSTAL_CODE] == "1234AB"
    assert result["options"][CONF_PARCELS] == []


async def test_invalid_postcode_shows_error(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_POSTAL_CODE: "invalid_postcode"}


async def test_duplicate_postcode_aborts(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="1234AB").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTAL_CODE: "1234AB"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- options flow ----------------------------------------------------------


def _entry(hass, parcels=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234AB",
        options={
            CONF_POSTAL_CODE: "1234AB",
            CONF_PARCELS: parcels or [],
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_REFRESH_INTERVAL: 30,
            CONF_INCLUDE_HISTORY: False,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _submit(add: str = "", remove=None) -> dict:
    # ``remove`` only exists in the schema once at least one parcel is tracked,
    # so it must be omitted entirely when there is nothing to remove.
    parcels_section: dict = {"add": add}
    if remove is not None:
        parcels_section["remove"] = remove
    return {
        "parcels": parcels_section,
        "delivered": {
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
        "history": {CONF_INCLUDE_HISTORY: False},
        "polling": {CONF_REFRESH_INTERVAL: "30"},
    }


async def test_add_parcel_verified_against_trunkrs(hass):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(_VERIFY, new=AsyncMock(return_value=True)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _submit(add="tr123456")
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}
    ]


async def test_add_parcel_rejected_when_trunkrs_does_not_know_it(hass):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(_VERIFY, new=AsyncMock(return_value=False)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _submit(add="TR999999")
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown_parcel"}


async def test_add_parcel_accepted_when_trunkrs_is_unreachable(hass):
    """An outage must not stop a user adding a parcel they know is valid."""
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(_VERIFY, new=AsyncMock(return_value=None)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _submit(add="TR123456")
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"][CONF_PARCELS]) == 1


async def test_add_invalid_number_shows_error(hass):
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _submit(add="!!")
    )
    assert result["errors"] == {"base": "invalid_trunkrs_nr"}


async def test_add_duplicate_shows_error(hass):
    entry = _entry(
        hass, parcels=[{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}]
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _submit(add="TR123456")
    )
    assert result["errors"] == {"base": "already_tracked"}


async def test_remove_then_add_same_number_works(hass):
    """Removal is applied before the add, so re-adding in one submit works."""
    entry = _entry(
        hass, parcels=[{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}]
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch(_VERIFY, new=AsyncMock(return_value=True)):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], _submit(add="TR123456", remove=["TR123456"])
        )

    assert result["data"][CONF_PARCELS] == [
        {CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}
    ]


async def test_remove_parcel(hass):
    entry = _entry(
        hass, parcels=[{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}]
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _submit(remove=["TR123456"])
    )
    assert result["data"][CONF_PARCELS] == []
