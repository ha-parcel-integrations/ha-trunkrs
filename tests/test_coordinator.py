"""Tests for the Trunkrs coordinator logic."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.api import TrunkrsApiError, TrunkrsAuthError
from custom_components.trunkrs.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    KNOWN_CAPABILITIES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
    ParcelStatus,
)
from custom_components.trunkrs.coordinator import (
    TrunkrsCoordinator,
    _hottest_tier_minutes,
    _in_quiet_window,
    _next_anchor,
    _next_update_interval,
    _stagger_minutes,
)
from custom_components.trunkrs.parcels import (
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


# ---------------------------------------------------------------------------
# Dynamic polling (dynamic-polling.md Section 2.1, barcode-based) — pure
# helpers
# ---------------------------------------------------------------------------


def test_quiet_window_is_midnight_to_six():
    assert _in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert _in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not _in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def test_next_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_next_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert _next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_stagger_is_stable_and_bounded():
    a = _stagger_minutes("entry-1")
    b = _stagger_minutes("entry-1")
    c = _stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


def test_tier_is_none_when_nothing_active():
    assert _hottest_tier_minutes([], datetime(2026, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_mid_for_non_hot_statuses():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "registered", "planned_from": None},
        {"status": "unknown", "planned_from": None},
        {"status": "returning", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_tier_is_hot_when_out_for_delivery_without_planned_from():
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    parcels = [
        {"status": "unknown", "planned_from": None},
        {"status": "out_for_delivery", "planned_from": None},
    ]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_hot_within_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(minutes=30)  # inside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == HOT_INTERVAL_MINUTES


def test_tier_is_mid_before_lookahead_of_planned_from():
    planned = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    now = planned - timedelta(hours=3)  # well outside the 1h lookahead
    parcels = [{"status": "out_for_delivery", "planned_from": planned.isoformat()}]
    assert _hottest_tier_minutes(parcels, now) == MID_INTERVAL_MINUTES


def test_next_update_interval_is_none_for_none_tier():
    assert _next_update_interval(datetime(2026, 1, 1, 12, tzinfo=UTC), None, "entry-1") is None


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = _stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_to_next_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = _next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landing_in_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = _next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Dynamic polling — wired into _async_update_data
# ---------------------------------------------------------------------------


async def test_polling_stops_entirely_with_nothing_tracked(hass):
    entry = _entry(hass, parcels=[])
    client = AsyncMock()
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval is None


async def test_polling_stops_when_everything_delivered(hass):
    entry = _entry(hass)
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value=DELIVERED)
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes is None
    assert coordinator.update_interval is None


async def test_polling_is_mid_for_a_non_out_for_delivery_active_parcel(hass):
    """No confirmed Trunkrs status maps to ``out_for_delivery`` yet (see
    parcels.py) — an unmapped status reports ``unknown``, which is still an
    active, not-yet-delivered parcel and lands on the mid tier."""
    entry = _entry(hass)
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(return_value=IN_TRANSIT)
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_polling_is_hot_for_an_out_for_delivery_parcel(hass, monkeypatch):
    """No raw Trunkrs status has been confirmed to map to
    ``out_for_delivery`` yet (see parcels.py's ``_STATUS_MAP``), so this
    temporarily maps one to exercise the hot tier the same way it will work
    once a real one is confirmed — the tier logic itself is carrier-agnostic."""
    from custom_components.trunkrs import parcels as parcels_module

    monkeypatch.setitem(
        parcels_module._STATUS_MAP,
        "SHIPMENT_OUT_FOR_DELIVERY",
        ParcelStatus.OUT_FOR_DELIVERY,
    )
    entry = _entry(hass)
    client = AsyncMock()
    client.async_get_parcel = AsyncMock(
        return_value={
            **IN_TRANSIT,
            "currentState": {
                "stateName": "SHIPMENT_OUT_FOR_DELIVERY",
                "setAt": "2026-07-10T11:07:46.198Z",
                "reasonCode": None,
            },
        }
    )
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == HOT_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


async def test_polling_resumes_when_a_parcel_is_added_back(hass):
    """Adding a parcel back after a full stop re-arms scheduling on the next
    refresh, via the same options-update-triggered refresh path."""
    entry = _entry(hass, parcels=[])
    client = AsyncMock()
    coordinator = _coordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert coordinator.update_interval is None

    client.async_get_parcel = AsyncMock(return_value=IN_TRANSIT)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [{CONF_TRUNKRS_NR: "TR123", CONF_POSTAL_CODE: "1234AB"}],
        },
    )
    await coordinator._async_update_data()

    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.update_interval is not None


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


def test_accepted_by_driver_maps_to_out_for_delivery():
    """Confirmed in issue #6: the driver has the parcel on today's route."""
    payload = {**DELIVERED, "currentState": {"stateName": "SHIPMENT_ACCEPTED_BY_DRIVER"}}
    parcel = normalize_parcel(payload, trunkrs_nr="TR123")
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_tracking_url_deep_links_with_the_postal_code():
    """Confirmed in issue #7: number + postcode links straight to the parcel."""
    parcel = normalize_parcel(DELIVERED, trunkrs_nr="TR123", postal_code="1234AB")
    assert parcel["url"] == "https://parcel.trunkrs.nl/TR123/1234AB"


def test_tracking_url_falls_back_without_a_postal_code():
    parcel = normalize_parcel(DELIVERED, trunkrs_nr="TR123")
    assert parcel["url"] == "https://parcel.trunkrs.nl/"


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


def test_map_parcel_status_sorted_is_in_transit():
    """Confirmed in issue #5: SHIPMENT_SORTED is a sort-facility state."""
    assert map_parcel_status("SHIPMENT_SORTED") == ParcelStatus.IN_TRANSIT


def test_map_parcel_status_unmapped_warns_once(caplog):
    from custom_components.trunkrs import parcels

    parcels._unmapped_statuses_logged.clear()
    assert map_parcel_status("SOMETHING_NEW") == ParcelStatus.UNKNOWN
    assert map_parcel_status("SOMETHING_NEW") == ParcelStatus.UNKNOWN
    assert caplog.text.count("Unrecognised Trunkrs status") == 1
    assert "issues/new" in caplog.text


def test_map_event_status_returns_none_for_unmapped():
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None


def test_reason_code_warns_once(caplog):
    """A delivery reasonCode (the failed-state vocabulary we still need) is
    flagged once, with the state:reason pair and an issue link."""
    from custom_components.trunkrs import parcels

    parcels._reason_code_logged = False
    raw = {
        "currentState": {"stateName": "SHIPMENT_NOT_DELIVERED", "reasonCode": "NOBODY_HOME"},
        "deliveryAttempts": [],
    }
    normalize_parcel(raw, trunkrs_nr="TR123")
    normalize_parcel(raw, trunkrs_nr="TR123")
    assert caplog.text.count("delivery reasonCode we have not mapped") == 1
    assert "SHIPMENT_NOT_DELIVERED:NOBODY_HOME" in caplog.text
    assert "issues/new" in caplog.text


def test_reason_code_absent_is_silent(caplog):
    """A payload without a reasonCode logs nothing."""
    from custom_components.trunkrs import parcels

    parcels._reason_code_logged = False
    normalize_parcel(DELIVERED, trunkrs_nr="TR123")
    assert "delivery reasonCode" not in caplog.text


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


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_are_delivery_window_url_and_history():
    """Home-delivery courier: no pickup point, weight or dimensions in the payload."""
    assert CAPABILITIES == {"delivery_window", "url", "history"}
