"""Tests for the Trunkrs coordinator logic."""
from unittest.mock import AsyncMock

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.trunkrs.api import TrunkrsApiError, TrunkrsAuthError
from custom_components.trunkrs.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
    ParcelStatus,
)
from custom_components.trunkrs.coordinator import (
    TrunkrsCoordinator,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    sort_parcels_by_ts,
)

from .payloads import DELIVERED, IN_TRANSIT

_PAYLOAD = DELIVERED


def _entry(hass, parcels=None, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234AB",
        options={
            CONF_POSTAL_CODE: "1234AB",
            CONF_PARCELS: parcels
            if parcels is not None
            else [{CONF_TRUNKRS_NR: "TR123", CONF_POSTAL_CODE: "1234AB"}],
            **(options or {}),
        },
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass, client, entry) -> TrunkrsCoordinator:
    return TrunkrsCoordinator(hass, client, entry)


# --- normalize_parcel: the canonical contract ------------------------------

_CANONICAL_KEYS = {
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
}


def test_normalize_publishes_the_full_canonical_key_set():
    """Every suite carrier publishes exactly these keys — no more, no less."""
    parcel = normalize_parcel(_PAYLOAD, trunkrs_nr="TR123")
    assert set(parcel) == _CANONICAL_KEYS


def test_normalize_falls_back_to_the_entered_number_for_the_barcode():
    """The Trunkrs number is half of the credential pair, so it is always known.

    A sparse or empty response must still yield an identifiable parcel.
    """
    parcel = normalize_parcel({}, trunkrs_nr="TR123")
    assert parcel["barcode"] == "TR123"
    assert parcel["carrier"] == "Trunkrs"


def test_barcode_is_the_entered_number_not_the_payloads():
    """The barcode drives the sensor's unique_id, so it must never change.

    The entered number exists before the first successful poll; deriving it
    from the payload would churn the entity (and its history) the moment data
    arrives.
    """
    parcel = normalize_parcel(DELIVERED, trunkrs_nr="TR-ENTERED")
    assert parcel["barcode"] == "TR-ENTERED"
    assert parcel["raw"]["trunkrsNr"] == "419719666"


def test_normalize_preserves_the_raw_payload_verbatim():
    """``raw`` keeps the untouched response for diagnostics — never mutate it."""
    parcel = normalize_parcel(_PAYLOAD, trunkrs_nr="TR123")
    assert parcel["raw"] == _PAYLOAD


def test_normalize_maps_a_delivered_parcel():
    parcel = normalize_parcel(DELIVERED, trunkrs_nr="TR123")
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "SHIPMENT_DELIVERED"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-07-10T17:46:17.864Z"
    assert parcel["sender"] == "ExampleShop"
    assert parcel["receiver"] == "John Doe"


def test_delivered_parcel_clears_the_delivery_window():
    """Matches the other suite carriers: no ETA once it has arrived."""
    parcel = normalize_parcel(DELIVERED, trunkrs_nr="TR123")
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_prefers_the_narrow_delivery_window():
    """timeSlot carries both windows; from/to is the live prediction."""
    payload = {**DELIVERED, "currentState": {"stateName": "IN_TRANSIT_X"}}
    parcel = normalize_parcel(payload, trunkrs_nr="TR123")
    assert parcel["planned_from"] == "2026-07-10T17:34:40.318Z"
    assert parcel["planned_to"] == "2026-07-10T18:00:55.318Z"


def test_normalize_falls_back_to_the_wide_delivery_window():
    """Before the tour is planned only low/high are populated."""
    parcel = normalize_parcel(IN_TRANSIT, trunkrs_nr="TR123")
    assert parcel["planned_from"] == "2026-07-10T15:00:00.000Z"
    assert parcel["planned_to"] == "2026-07-10T20:30:00.000Z"


def test_unmapped_state_reports_unknown_but_stays_undelivered():
    """An unmapped status must never be filed away as delivered."""
    parcel = normalize_parcel(IN_TRANSIT, trunkrs_nr="TR123")
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "SHIPMENT_SOME_UNMAPPED_STATE"
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_trunkrs_exposes_no_pickup_or_weight():
    """Home-delivery courier: no ServicePoint, no weight/dimensions."""
    parcel = normalize_parcel(DELIVERED, trunkrs_nr="TR123")
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


def test_normalize_history_is_opt_in():
    assert normalize_parcel(DELIVERED, trunkrs_nr="TR123")["history"] is None
    assert normalize_parcel(
        DELIVERED, trunkrs_nr="TR123", include_history=True
    )["history"] == [
        {
            "timestamp": "2026-07-10T17:46:17.864Z",
            "status": ParcelStatus.DELIVERED,
            "raw_status": "SHIPMENT_DELIVERED",
        }
    ]


def test_build_history_reads_delivery_attempts_oldest_first():
    raw = {
        "deliveryAttempts": [
            {"stateName": "SHIPMENT_DELIVERED", "setAt": "2026-07-10T17:46:17.864Z"},
            {"stateName": "SHIPMENT_X", "setAt": "2026-07-10T09:00:00.000Z"},
        ]
    }
    history = build_history(raw)
    assert [e["timestamp"] for e in history] == [
        "2026-07-10T09:00:00.000Z",
        "2026-07-10T17:46:17.864Z",
    ]
    assert history[0]["status"] is None  # unmapped -> null, not a guess
    assert history[1]["status"] == ParcelStatus.DELIVERED


def test_build_history_ignores_junk_entries_and_caps_the_list():
    raw = {"deliveryAttempts": ["not-a-dict", {"stateName": "X"}, {"setAt": ""}]}
    assert build_history(raw) == []
    many = {
        "deliveryAttempts": [
            {"stateName": "X", "setAt": f"2026-07-10T{h:02d}:00:00.000Z"}
            for h in range(23)
        ]
    }
    assert len(build_history(many, max_events=20)) == 20


def test_build_history_handles_a_payload_without_attempts():
    assert build_history({}) == []
    assert build_history(None) == []


# --- status mapping --------------------------------------------------------


def test_map_parcel_status_none_is_silently_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_warns_once(caplog):
    from custom_components.trunkrs import coordinator as coord

    coord._unmapped_statuses_logged.clear()
    assert map_parcel_status("SOMETHING_NEW") == ParcelStatus.UNKNOWN
    assert map_parcel_status("SOMETHING_NEW") == ParcelStatus.UNKNOWN
    assert caplog.text.count("Unrecognised Trunkrs status") == 1
    assert "issues/new" in caplog.text


def test_map_event_status_returns_none_for_unmapped():
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None


# --- sorting ---------------------------------------------------------------


def test_sort_puts_missing_timestamps_last_in_both_directions():
    parcels = [
        {"barcode": "b", "planned_from": None},
        {"barcode": "a", "planned_from": "2026-05-01T10:00:00Z"},
        {"barcode": "c", "planned_from": "2026-05-02T10:00:00Z"},
    ]
    ascending = sort_parcels_by_ts(parcels, "planned_from")
    assert [p["barcode"] for p in ascending] == ["a", "c", "b"]
    descending = sort_parcels_by_ts(parcels, "planned_from", descending=True)
    assert [p["barcode"] for p in descending] == ["c", "a", "b"]


def test_sort_treats_unparseable_timestamp_as_missing():
    parcels = [
        {"barcode": "bad", "planned_from": "not-a-date"},
        {"barcode": "good", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    assert [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")] == [
        "good",
        "bad",
    ]


# --- delivered filter ------------------------------------------------------


async def test_delivered_filter_by_count(hass):
    entry = _entry(
        hass,
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 2,
        },
    )
    coordinator = _coordinator(hass, AsyncMock(), entry)
    parcels = [{"delivered_at": f"2026-05-0{i}T10:00:00Z"} for i in (3, 2, 1)]
    assert len(coordinator._apply_delivered_filter(parcels)) == 2


async def test_delivered_filter_by_days_keeps_unparseable(hass):
    entry = _entry(
        hass,
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
        },
    )
    coordinator = _coordinator(hass, AsyncMock(), entry)
    parcels = [
        {"delivered_at": "1999-01-01T10:00:00Z"},  # far too old → dropped
        {"delivered_at": None},                     # unknown → kept
    ]
    kept = coordinator._apply_delivered_filter(parcels)
    assert kept == [{"delivered_at": None}]


# --- update loop -----------------------------------------------------------


async def test_update_fetches_each_tracked_pair(hass):
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value={})
    entry = _entry(
        hass,
        parcels=[
            {CONF_TRUNKRS_NR: "TR1", CONF_POSTAL_CODE: "1234AB"},
            {CONF_TRUNKRS_NR: "TR2", CONF_POSTAL_CODE: "5678CD"},
        ],
    )
    coordinator = _coordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert {p["barcode"] for p in data} == {"TR1", "TR2"}
    assert client.async_get_parcel.await_count == 2
    assert coordinator.last_success_time is not None


async def test_update_keeps_parcel_visible_when_a_fetch_fails(hass):
    """A transient failure must not make the parcel's sensor disappear."""
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value=IN_TRANSIT)
    entry = _entry(hass)
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()  # populates the cache
    client.async_get_parcel = AsyncMock(side_effect=TrunkrsApiError(500))
    data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data] == ["TR123"]
    assert data[0]["raw"] == IN_TRANSIT  # served from cache


async def test_update_reports_auth_failure_clearly(hass, caplog):
    """A bad number/postcode pair is the user's problem, not an outage."""
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(side_effect=TrunkrsAuthError(401))
    coordinator = _coordinator(hass, client, _entry(hass))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert "check the number and postcode" in caplog.text


async def test_update_raises_when_everything_fails(hass):
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    coordinator = _coordinator(hass, client, _entry(hass))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_propagates_unexpected_exceptions(hass):
    """Only known API/network errors are swallowed — bugs must surface."""
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(side_effect=RuntimeError("bug"))
    coordinator = _coordinator(hass, client, _entry(hass))

    with pytest.raises(RuntimeError):
        await coordinator._async_update_data()


async def test_update_drops_cache_for_untracked_parcels(hass):
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value=_PAYLOAD)
    entry = _entry(hass)
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert "TR123" in coordinator._raw_cache

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PARCELS: []}
    )
    await coordinator._async_update_data()
    assert coordinator._raw_cache == {}


async def test_no_tracked_parcels_is_not_a_failure(hass):
    client = AsyncMock()
    coordinator = _coordinator(hass, client, _entry(hass, parcels=[]))

    assert await coordinator._async_update_data() == []
    assert coordinator.last_success_time is not None


# --- events ----------------------------------------------------------------


async def test_events_are_suppressed_on_the_first_refresh(hass):
    """We cannot tell "new" from "already existed" on the first poll."""
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value=_PAYLOAD)
    coordinator = _coordinator(hass, client, _entry(hass))

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)

    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert events == []


async def test_registered_event_fires_for_a_new_barcode(hass):
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value={})
    entry = _entry(hass)
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()  # first refresh: silent

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", events.append)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRUNKRS_NR: "TR123", CONF_POSTAL_CODE: "1234AB"},
                {CONF_TRUNKRS_NR: "TR999", CONF_POSTAL_CODE: "1234AB"},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["barcode"] for e in events] == ["TR999"]
    assert "device_id" in events[0].data
