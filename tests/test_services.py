"""Tests for the Trunkrs track/untrack services."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.const import (
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
)
from custom_components.trunkrs.services import (
    SERVICE_TRACK_PARCEL,
    SERVICE_UNTRACK_PARCEL,
    async_setup_services,
)

_VERIFY = "custom_components.trunkrs.services.async_verify_parcel"


def _entry(hass, postcode="1234AB", parcels=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=postcode,
        options={CONF_POSTAL_CODE: postcode, CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    return entry


async def _call(hass, service, data):
    await hass.services.async_call(DOMAIN, service, data, blocking=True)


async def test_track_adds_a_verified_parcel(hass):
    entry = _entry(hass)
    async_setup_services(hass)

    with patch(_VERIFY, new=AsyncMock(return_value=True)):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "tr123456"})

    assert entry.options[CONF_PARCELS] == [
        {CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}
    ]


async def test_track_rejects_a_parcel_trunkrs_does_not_know(hass):
    _entry(hass)
    async_setup_services(hass)

    with patch(_VERIFY, new=AsyncMock(return_value=False)):
        with pytest.raises(ServiceValidationError):
            await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "TR999999"})


async def test_track_accepts_when_trunkrs_is_unreachable(hass):
    """An outage must not block tracking a parcel the user knows is valid."""
    entry = _entry(hass)
    async_setup_services(hass)

    with patch(_VERIFY, new=AsyncMock(return_value=None)):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "TR123456"})

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_track_is_a_noop_for_an_already_tracked_parcel(hass):
    entry = _entry(
        hass, parcels=[{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}]
    )
    async_setup_services(hass)

    # Must not even reach the verify call.
    with patch(_VERIFY, new=AsyncMock(side_effect=AssertionError("should not verify"))):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "TR123456"})

    assert len(entry.options[CONF_PARCELS]) == 1


async def test_track_rejects_an_invalid_number(hass):
    _entry(hass)
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "!!"})


async def test_track_without_a_hub_raises(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "TR123456"})


async def test_track_selects_the_hub_by_postal_code(hass):
    _entry(hass, postcode="1234AB")
    second = _entry(hass, postcode="5678CD")
    async_setup_services(hass)

    with patch(_VERIFY, new=AsyncMock(return_value=True)):
        await _call(
            hass,
            SERVICE_TRACK_PARCEL,
            {CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "5678CD"},
        )

    assert len(second.options[CONF_PARCELS]) == 1


async def test_track_is_ambiguous_with_several_hubs_and_no_postcode(hass):
    _entry(hass, postcode="1234AB")
    _entry(hass, postcode="5678CD")
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRUNKRS_NR: "TR123456"})


async def test_track_with_an_unknown_postcode_raises(hass):
    _entry(hass, postcode="1234AB")
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            SERVICE_TRACK_PARCEL,
            {CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "9999ZZ"},
        )


async def test_untrack_removes_the_parcel(hass):
    entry = _entry(
        hass, parcels=[{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}]
    )
    async_setup_services(hass)

    await _call(hass, SERVICE_UNTRACK_PARCEL, {CONF_TRUNKRS_NR: "tr123456"})

    assert entry.options[CONF_PARCELS] == []


async def test_untrack_without_a_hub_raises(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_UNTRACK_PARCEL, {CONF_TRUNKRS_NR: "TR123456"})
