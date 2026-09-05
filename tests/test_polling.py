"""Tests for Section 2.1's dynamic, status-driven polling algorithm.

Pure-function tests for the tiering/scheduling helpers, plus a few
integration checks that ``_async_update_data`` actually wires them up.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.const import (
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
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

from .payloads import DELIVERED, IN_TRANSIT


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
