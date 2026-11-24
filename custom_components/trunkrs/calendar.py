"""Calendar platform for the Trunkrs parcel tracker integration."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import TrunkrsConfigEntry
from .coordinator import TrunkrsCoordinator
from .device import ATTRIBUTION, build_device_info
from .parcels import parse_iso

PARALLEL_UPDATES = 0

_DEFAULT_EVENT_DURATION = timedelta(hours=1)
_END_OF_DAY = time(23, 59, 59)


def _whole_days(start: datetime, end: datetime) -> tuple[date, date] | None:
    """Return the days a 00:00:00 to 23:59:59 window spans, else ``None``.

    Checked in the window's own offset first, so a carrier's local or naive
    wall clock counts, then in Home Assistant's zone for a window sent in UTC.
    The end date is exclusive, as Home Assistant expects for all-day events.
    """
    for first, last in ((start, end), (dt_util.as_local(start), dt_util.as_local(end))):
        if (
            first.time().replace(tzinfo=None) == time.min
            and last.time().replace(tzinfo=None, microsecond=0) == _END_OF_DAY
            and last.date() >= first.date()
        ):
            return first.date(), last.date() + timedelta(days=1)
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrunkrsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Trunkrs deliveries calendar from a config entry."""
    async_add_entities(
        [TrunkrsDeliveriesCalendar(entry.runtime_data.coordinator, entry)]
    )


class TrunkrsDeliveriesCalendar(CoordinatorEntity[TrunkrsCoordinator], CalendarEntity):
    """A read-only calendar of expected Trunkrs deliveries.

    Each active tracked parcel with a known delivery moment becomes an event.
    No extra API calls — a pure view over coordinator data — so it is enabled
    by default and can be turned off per entity if unwanted.

    Note: until the payload mapping is finished (see ``coordinator.py``),
    ``planned_from`` is always ``None``, so this calendar stays empty. It is
    included now so it starts working the moment the mapping lands.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "deliveries"
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: TrunkrsCoordinator, entry: ConfigEntry) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deliveries"
        self._attr_device_info = build_device_info(entry)

    def _events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for parcel in self.coordinator.data or []:
            start = parse_iso(parcel.get("planned_from"))
            if start is None:
                continue
            end = parse_iso(parcel.get("planned_to"))
            if end is None or end <= start:
                end = start + _DEFAULT_EVENT_DURATION
            event_start: datetime | date = start
            event_end: datetime | date = end
            if days := _whole_days(start, end):
                event_start, event_end = days

            barcode = parcel.get("barcode") or ""
            # Contents say what is arriving; the sender is the fallback when the
            # parcel has none.
            summary = (
                parcel.get("contents")
                or parcel.get("sender")
                or (f"Parcel {barcode}" if barcode else "Trunkrs parcel")
            )
            description_parts = [
                f"Barcode: {barcode}" if barcode else None,
                f"Status: {parcel.get('status')}" if parcel.get("status") else None,
                parcel.get("url"),
            ]
            description = "\n".join(p for p in description_parts if p)
            location = parcel.get("pickup_point") if parcel.get("pickup") else None

            events.append(
                CalendarEvent(
                    start=event_start,
                    end=event_end,
                    summary=summary,
                    description=description or None,
                    location=location,
                    uid=barcode or None,
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming calendar event."""
        now = dt_util.now()
        upcoming = [event for event in self._events() if event.end_datetime_local > now]
        return (
            min(upcoming, key=lambda event: event.start_datetime_local)
            if upcoming
            else None
        )

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        return [
            event
            for event in self._events()
            if event.start_datetime_local < end_date
            and event.end_datetime_local > start_date
        ]
