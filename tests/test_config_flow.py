"""Tests for the Trunkrs config and options flows."""
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


async def _open_options_step(hass, entry, step_id: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_parcel_list_can_be_cleared(hass):
    entry = _entry(hass, parcels=[{CONF_TRUNKRS_NR: "TR123456"}])
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["data"][CONF_PARCELS] == []


async def test_options_settings_preserve_parcel_list(hass):
    parcels = [{CONF_TRUNKRS_NR: "TR123456"}]
    entry = _entry(hass, parcels=parcels)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
            CONF_REFRESH_INTERVAL: "30",
        },
    )
    assert result["data"][CONF_PARCELS] == parcels
