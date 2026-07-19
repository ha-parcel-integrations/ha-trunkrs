"""Tests for Trunkrs setup and unload."""
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
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
)

from .payloads import IN_TRANSIT as _PAYLOAD
_GET = "custom_components.trunkrs.api.TrunkrsApiClient.async_get_parcel"


def _entry(hass, postcode="1234AB") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=postcode,
        options={
            CONF_POSTAL_CODE: postcode,
            CONF_PARCELS: [{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: postcode}],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_and_unload(hass):
    entry = _entry(hass)

    with patch(_GET, new=AsyncMock(return_value=_PAYLOAD)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    incoming = hass.states.get("sensor.trunkrs_1234ab_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    # Services are registered while a hub is loaded.
    assert hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)
    assert hass.services.has_service(DOMAIN, SERVICE_UNTRACK_PARCEL)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    # Last hub gone → services removed.
    assert not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)


async def test_setup_retries_when_the_first_refresh_fails(hass):
    """The first refresh runs in __init__ before platforms are forwarded.

    A failure there raises ConfigEntryNotReady from the entry setup itself
    (SETUP_RETRY) instead of half-setting-up the entry from a forwarded
    platform.
    """
    entry = _entry(hass)

    with patch(_GET, new=AsyncMock(side_effect=aiohttp.ClientError("boom"))):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_services_survive_unloading_one_of_two_hubs(hass):
    """Services are shared, so they only go away with the last hub."""
    first = _entry(hass, postcode="1234AB")
    second = _entry(hass, postcode="5678CD")

    with patch(_GET, new=AsyncMock(return_value=_PAYLOAD)):
        # Setting up the component loads every entry of the domain, so this
        # one call brings both hubs up.
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()

    assert second.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL)


async def test_changing_options_refreshes_without_reloading(hass):
    """Adding a parcel applies live via the update listener (no reload)."""
    entry = _entry(hass)

    with patch(_GET, new=AsyncMock(return_value=_PAYLOAD)) as mock_get:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        calls_after_setup = mock_get.await_count

        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [
                    {CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"},
                    {CONF_TRUNKRS_NR: "TR999999", CONF_POSTAL_CODE: "1234AB"},
                ],
            },
        )
        await hass.async_block_till_done()

        assert mock_get.await_count > calls_after_setup

    # Still loaded — an update listener that reloaded would have torn it down.
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.trunkrs_1234ab_incoming_parcels").state == "2"
