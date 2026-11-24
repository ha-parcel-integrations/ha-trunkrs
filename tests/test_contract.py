"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.trunkrs.const import ParcelStatus
from custom_components.trunkrs.parcels import (
    is_pickup,
    normalize_parcel,
    planned_for_status,
    planned_window,
)

from .payloads import DELIVERED, IN_TRANSIT

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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
]

_AMS = ZoneInfo("Europe/Amsterdam")


def _slot(**slot):
    return {**IN_TRANSIT, "timeSlot": slot}


SAMPLES = {
    "in_transit": lambda: IN_TRANSIT,
    "delivered": lambda: DELIVERED,
    "narrow_window": lambda: _slot(
        low="2099-07-10T15:00:00.000Z",
        high="2099-07-10T20:30:00.000Z",
        **{"from": "2099-07-10T17:34:00.000Z", "to": "2099-07-10T18:00:00.000Z"},
    ),
    "half_narrow": lambda: _slot(
        low="2099-07-10T15:00:00.000Z",
        high="2099-07-10T20:30:00.000Z",
        **{"from": "2099-07-10T17:34:00.000Z", "to": None},
    ),
    "no_slot": lambda: {**IN_TRANSIT, "timeSlot": None},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(
        SAMPLES[request.param](), trunkrs_nr="1", postal_code="1234AB"
    )


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert datetime.fromisoformat(parcel["planned_to"]) > datetime.fromisoformat(
            parcel["planned_from"]
        )


def test_planned_is_empty_when_the_parcel_is_done(parcel):
    if parcel["delivered"]:
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_constant_false(parcel):
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_the_narrow_window_wins_as_a_pair():
    parcel = normalize_parcel(SAMPLES["narrow_window"](), trunkrs_nr="1")
    assert parcel["planned_from"] == "2099-07-10T17:34:00.000Z"
    assert parcel["planned_to"] == "2099-07-10T18:00:00.000Z"


def test_a_half_narrow_window_falls_back_to_the_wide_pair():
    parcel = normalize_parcel(SAMPLES["half_narrow"](), trunkrs_nr="1")
    assert parcel["planned_from"] == "2099-07-10T15:00:00.000Z"
    assert parcel["planned_to"] == "2099-07-10T20:30:00.000Z"


def test_a_narrow_start_is_never_mixed_with_a_wide_end():
    parcel = normalize_parcel(
        _slot(high="2099-07-10T20:30:00.000Z", **{"from": "2099-07-10T17:34:00.000Z"}),
        trunkrs_nr="1",
    )
    assert parcel["planned_from"] == "2099-07-10T20:30:00.000Z"
    assert "17:34" not in str(parcel["planned_from"])
    assert "17:34" not in str(parcel["planned_to"])


def test_a_slot_without_an_end_runs_to_the_end_of_the_local_day():
    parcel = normalize_parcel(_slot(low="2099-07-10T15:00:00.000Z"), trunkrs_nr="1")
    assert parcel["planned_to"] == "2099-07-10T23:59:59+02:00"


def test_reversed_slot_ends_are_swapped():
    parcel = normalize_parcel(
        _slot(low="2099-07-10T20:00:00.000Z", high="2099-07-10T15:00:00.000Z"),
        trunkrs_nr="1",
    )
    assert parcel["planned_from"] < parcel["planned_to"]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T15:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29T13:00:00",
            None,
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            None,
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            date(2026, 4, 29),
            date(2026, 4, 29),
            ("2026-04-29T00:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            "2026-04-29",
            "2026-05-01",
            ("2026-04-29T00:00:00+02:00", "2026-05-01T23:59:59+02:00"),
        ),
        (
            "2026-04-29T15:00:00+02:00",
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (
            None,
            "2026-04-29T13:00:00+02:00",
            ("2026-04-29T13:00:00+02:00", "2026-04-29T23:59:59+02:00"),
        ),
        (
            datetime(2026, 4, 29, 13, 0),
            datetime(2026, 4, 29, 15, 0),
            ("2026-04-29T13:00:00+02:00", "2026-04-29T15:00:00+02:00"),
        ),
        (None, None, (None, None)),
        ("garbage", None, (None, None)),
        (12, 13, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=_AMS) == expected


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED],
)
def test_planned_for_status_empties_the_window(status):
    assert planned_for_status(status, "a", "b") == (None, None)


def test_planned_for_status_keeps_the_window_otherwise():
    for status in (
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.UNKNOWN,
    ):
        assert planned_for_status(status, "a", "b") == ("a", "b")


def test_is_pickup_sources():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.AT_PICKUP_POINT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [None, ParcelStatus.AT_PICKUP_POINT])
    assert is_pickup(False, ParcelStatus.IN_TRANSIT, [ParcelStatus.IN_TRANSIT]) is False
    assert is_pickup(None, ParcelStatus.DELIVERED) is False
