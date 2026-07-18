"""Tests for the Trunkrs bus events.

``_fire_change_events`` works on *normalised* parcels, so the full event
contract can be verified today by driving it directly — independent of the
missing payload mapping.
"""
from unittest.mock import AsyncMock

import aiohttp
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.api import TrunkrsApiError
from custom_components.trunkrs.config_flow import async_verify_parcel
from custom_components.trunkrs.const import (
    CONF_POSTAL_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.trunkrs.coordinator import TrunkrsCoordinator


def _coordinator(hass) -> TrunkrsCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1234AB", options={CONF_POSTAL_CODE: "1234AB"}
    )
    entry.add_to_hass(hass)
    return TrunkrsCoordinator(hass, AsyncMock(), entry)


def _parcel(barcode="TR1", status=ParcelStatus.IN_TRANSIT, **extra) -> dict:
    return {
        "barcode": barcode,
        "status": status,
        "planned_from": None,
        "planned_to": None,
        **extra,
    }


def _listen(hass, event: str) -> list:
    captured: list = []
    hass.bus.async_listen(f"{DOMAIN}_{event}", captured.append)
    return captured


async def test_status_change_fires_status_changed(hass):
    coordinator = _coordinator(hass)
    coordinator._known_state = {"TR1": ParcelStatus.REGISTERED}
    events = _listen(hass, "parcel_status_changed")

    coordinator._fire_change_events([_parcel(status=ParcelStatus.IN_TRANSIT)])
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.REGISTERED
    assert events[0].data["new_status"] == ParcelStatus.IN_TRANSIT


async def test_delivery_fires_only_the_delivered_event(hass):
    """The terminal hop fires exactly one, dedicated event — never both."""
    coordinator = _coordinator(hass)
    coordinator._known_state = {"TR1": ParcelStatus.OUT_FOR_DELIVERY}
    delivered = _listen(hass, "parcel_delivered")
    changed = _listen(hass, "parcel_status_changed")

    coordinator._fire_change_events([_parcel(status=ParcelStatus.DELIVERED)])
    await hass.async_block_till_done()

    assert len(delivered) == 1
    assert changed == []


async def test_a_barcode_first_seen_delivered_fires_nothing(hass):
    coordinator = _coordinator(hass)
    coordinator._known_state = {}
    registered = _listen(hass, "parcel_registered")
    delivered = _listen(hass, "parcel_delivered")

    coordinator._fire_change_events([_parcel(status=ParcelStatus.DELIVERED)])
    await hass.async_block_till_done()

    assert registered == []
    assert delivered == []


async def test_unchanged_status_fires_nothing(hass):
    coordinator = _coordinator(hass)
    coordinator._known_state = {"TR1": ParcelStatus.IN_TRANSIT}
    events = _listen(hass, "parcel_status_changed")

    coordinator._fire_change_events([_parcel(status=ParcelStatus.IN_TRANSIT)])
    await hass.async_block_till_done()

    assert events == []


async def test_parcels_without_a_barcode_are_skipped(hass):
    coordinator = _coordinator(hass)
    coordinator._known_state = {}
    events = _listen(hass, "parcel_registered")

    coordinator._fire_change_events([_parcel(barcode=None)])
    await hass.async_block_till_done()

    assert events == []


async def test_new_eta_fires_delivery_time_changed(hass):
    coordinator = _coordinator(hass)
    coordinator._known_state = {"TR1": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"TR1": (None, None)}
    events = _listen(hass, "parcel_delivery_time_changed")

    coordinator._fire_change_events(
        [_parcel(planned_from="2026-05-01T10:00:00Z", planned_to="2026-05-01T12:00:00Z")]
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-05-01T10:00:00Z"
    assert events[0].data["old_planned_from"] is None


async def test_dropping_an_eta_is_intentionally_silent(hass):
    """value → null just means the carrier lost the window; not worth alerting."""
    coordinator = _coordinator(hass)
    coordinator._known_state = {"TR1": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"TR1": ("2026-05-01T10:00:00Z", None)}
    events = _listen(hass, "parcel_delivery_time_changed")

    coordinator._fire_change_events([_parcel(planned_from=None)])
    await hass.async_block_till_done()

    assert events == []


# --- verify helper error handling ------------------------------------------


async def test_verify_helper_returns_none_when_trunkrs_errors(hass):
    """None means "could not check" — callers then accept the parcel anyway."""
    client = AsyncMock()
    client.async_verify = AsyncMock(side_effect=TrunkrsApiError(500))
    with_patch = "custom_components.trunkrs.config_flow.TrunkrsApiClient"

    from unittest.mock import patch

    with patch(with_patch, return_value=client):
        assert await async_verify_parcel(hass, "TR1", "1234AB") is None


async def test_verify_helper_returns_none_on_network_error(hass):
    client = AsyncMock()
    client.async_verify = AsyncMock(side_effect=aiohttp.ClientError("boom"))

    from unittest.mock import patch

    with patch(
        "custom_components.trunkrs.config_flow.TrunkrsApiClient", return_value=client
    ):
        assert await async_verify_parcel(hass, "TR1", "1234AB") is None


async def test_verify_helper_passes_through_a_definite_answer(hass):
    client = AsyncMock()
    client.async_verify = AsyncMock(return_value=False)

    from unittest.mock import patch

    with patch(
        "custom_components.trunkrs.config_flow.TrunkrsApiClient", return_value=client
    ):
        assert await async_verify_parcel(hass, "TR1", "1234AB") is False
