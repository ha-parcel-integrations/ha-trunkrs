"""Tests for the Trunkrs deliveries calendar.

The calendar is payload-independent: it reads the *normalised* parcel dicts,
so it can be tested fully today by injecting parcels with delivery windows —
even though the field mapping that produces them is not written yet.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.calendar import TrunkrsDeliveriesCalendar
from custom_components.trunkrs.const import CONF_POSTAL_CODE, DOMAIN
from custom_components.trunkrs.coordinator import TrunkrsCoordinator


def _calendar(hass, parcels) -> TrunkrsDeliveriesCalendar:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1234AB", options={CONF_POSTAL_CODE: "1234AB"}
    )
    entry.add_to_hass(hass)
    coordinator = TrunkrsCoordinator(hass, AsyncMock(), entry)
    coordinator.data = parcels
    return TrunkrsDeliveriesCalendar(coordinator, entry)


def _parcel(barcode="TR1", start=None, end=None, **extra) -> dict:
    return {
        "barcode": barcode,
        "sender": None,
        "status": "in_transit",
        "url": "https://parcel.trunkrs.nl/",
        "planned_from": start,
        "planned_to": end,
        "pickup": False,
        "pickup_point": None,
        **extra,
    }


async def test_contents_is_the_event_title(hass):
    calendar = _calendar(
        hass,
        [_parcel(start=_iso(2), end=_iso(4), sender="Shop", contents="Garden hose")],
    )
    assert calendar._events()[0].summary == "Garden hose"


async def test_title_falls_back_to_sender_without_contents(hass):
    calendar = _calendar(
        hass,
        [_parcel(start=_iso(2), end=_iso(4), sender="Shop", contents=None)],
    )
    assert calendar._events()[0].summary == "Shop"


async def test_title_keeps_the_barcode_fallback_without_contents_or_sender(hass):
    calendar = _calendar(hass, [_parcel(start=_iso(2), end=_iso(4), contents=None)])
    assert calendar._events()[0].summary == "Parcel TR1"


# One fixed base instant, so offsets are exact — computing now() per call
# leaves microsecond drift between a start and its end.
_BASE = datetime.now(UTC)


def _iso(offset_hours: int) -> str:
    return (_BASE + timedelta(hours=offset_hours)).isoformat()


async def test_parcel_without_a_window_yields_no_event(hass):
    calendar = _calendar(hass, [_parcel()])
    assert calendar._events() == []
    assert calendar.event is None


async def test_event_uses_the_delivery_window(hass):
    calendar = _calendar(hass, [_parcel(start=_iso(2), end=_iso(4))])
    (event,) = calendar._events()
    assert (event.end - event.start) == timedelta(hours=2)


async def test_event_falls_back_to_a_one_hour_slot(hass):
    """Only a moment known → a sensible default duration, not a zero-length event."""
    calendar = _calendar(hass, [_parcel(start=_iso(2))])
    (event,) = calendar._events()
    assert (event.end - event.start) == timedelta(hours=1)


async def test_end_before_start_is_corrected(hass):
    calendar = _calendar(hass, [_parcel(start=_iso(4), end=_iso(2))])
    (event,) = calendar._events()
    assert event.end > event.start


async def test_summary_prefers_the_sender(hass):
    calendar = _calendar(hass, [_parcel(start=_iso(1), sender="Webshop")])
    assert calendar._events()[0].summary == "Webshop"


async def test_summary_falls_back_to_the_barcode(hass):
    calendar = _calendar(hass, [_parcel(barcode="TR9", start=_iso(1))])
    assert calendar._events()[0].summary == "Parcel TR9"


async def test_pickup_parcel_sets_the_location(hass):
    calendar = _calendar(
        hass,
        [_parcel(start=_iso(1), pickup=True, pickup_point="Service Point A")],
    )
    assert calendar._events()[0].location == "Service Point A"


async def test_description_lists_barcode_status_and_url(hass):
    calendar = _calendar(hass, [_parcel(start=_iso(1))])
    description = calendar._events()[0].description
    assert "TR1" in description
    assert "in_transit" in description
    assert "parcel.trunkrs.nl" in description


async def test_event_returns_the_soonest_upcoming(hass):
    calendar = _calendar(
        hass,
        [
            _parcel(barcode="later", start=_iso(10)),
            _parcel(barcode="sooner", start=_iso(2)),
        ],
    )
    assert calendar.event.summary == "Parcel sooner"


async def test_past_events_are_not_current(hass):
    calendar = _calendar(hass, [_parcel(start=_iso(-10), end=_iso(-9))])
    assert calendar.event is None


async def test_get_events_filters_by_range(hass):
    calendar = _calendar(
        hass,
        [
            _parcel(barcode="in", start=_iso(2)),
            _parcel(barcode="out", start=_iso(100)),
        ],
    )
    now = datetime.now(UTC)
    events = await calendar.async_get_events(hass, now, now + timedelta(hours=5))
    assert [e.summary for e in events] == ["Parcel in"]


async def test_unparseable_timestamp_is_skipped(hass):
    calendar = _calendar(hass, [_parcel(start="not-a-date")])
    assert calendar._events() == []


async def test_whole_day_window_is_an_all_day_event(hass):
    cal = _calendar(
        hass,
        [_parcel(start="2099-01-01T00:00:00+01:00", end="2099-01-01T23:59:59+01:00")],
    )
    event = cal._events()[0]
    assert (event.start, event.end) == (date(2099, 1, 1), date(2099, 1, 2))
    assert event.all_day


async def test_window_over_whole_days_is_one_all_day_event(hass):
    cal = _calendar(
        hass, [_parcel(start="2099-01-01T00:00:00", end="2099-01-03T23:59:59")]
    )
    event = cal._events()[0]
    assert (event.start, event.end) == (date(2099, 1, 1), date(2099, 1, 4))


async def test_whole_day_window_sent_in_utc_is_all_day_in_local_time(hass):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    cal = _calendar(
        hass, [_parcel(start="2099-01-01T23:00:00Z", end="2099-01-02T22:59:59Z")]
    )
    event = cal._events()[0]
    assert (event.start, event.end) == (date(2099, 1, 2), date(2099, 1, 3))


async def test_a_moment_stretched_to_the_end_of_its_day_stays_timed(hass):
    cal = _calendar(
        hass,
        [_parcel(start="2099-01-01T14:00:00+01:00", end="2099-01-01T23:59:59+01:00")],
    )
    assert not cal._events()[0].all_day


async def test_all_day_events_are_upcoming_and_filtered_by_range(hass):
    cal = _calendar(
        hass,
        [
            _parcel("PAST", start="2000-01-01T00:00:00", end="2000-01-01T23:59:59"),
            _parcel("FUTURE", start="2099-01-01T00:00:00", end="2099-01-01T23:59:59"),
        ],
    )
    assert cal.event.uid == "FUTURE"
    events = await cal.async_get_events(
        hass, datetime(2098, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    )
    assert {e.uid for e in events} == {"FUTURE"}
