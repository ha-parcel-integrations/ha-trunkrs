"""Coordinator for the Trunkrs parcel tracker integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TrunkrsApiClient, TrunkrsApiError, TrunkrsAuthError
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_INTERVAL,
    CONF_TRUNKRS_NR,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Trunkrs ``currentState.stateName`` -> canonical ParcelStatus.
#
# The payload shape was contributed in issue #1 (thanks @joerimul); see
# ``docs/api/tracing_details.md`` for the full sample and field mapping.
#
# ⚠️ The status vocabulary is still INCOMPLETE. Only ``SHIPMENT_DELIVERED`` has
# actually been observed. The ``SHIPMENT_*`` prefix suggests a family, but the
# other values are deliberately NOT guessed: a wrong guess would silently
# report the wrong status, whereas an unmapped one reports ``unknown`` and logs
# a one-shot warning with a copy-paste issue link — so the rest of the
# vocabulary collects itself from real users. Add each confirmed value here.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "SHIPMENT_DELIVERED": ParcelStatus.DELIVERED,
}

# The one state we can also act on structurally (``delivered`` / ``delivered_at``
# / clearing the ETA window), kept as a constant so the test and the mapping
# cannot drift apart.
STATE_DELIVERED = "SHIPMENT_DELIVERED"

# Raw statuses we have already warned about, so each unmapped one is logged
# only once per HA session.
_unmapped_statuses_logged: set[str] = set()


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


def _warn_unmapped_status(raw_status: str) -> None:
    """Log an unmapped Trunkrs status once, with a copy-paste issue link."""
    if raw_status in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(raw_status)
    _LOGGER.warning(
        "Unrecognised Trunkrs status — help us map it. Open an issue and paste "
        "this line: %s\n  status=%r → reported as 'unknown'",
        NEW_ISSUE_URL,
        raw_status,
    )


def map_parcel_status(raw_status: str | None) -> ParcelStatus:
    """Map a raw Trunkrs status to a canonical :class:`ParcelStatus`.

    ``None`` reports ``unknown`` silently; an unmapped non-null status reports
    ``unknown`` with a one-shot warning (the vocabulary is still incomplete —
    see ``_STATUS_MAP``).
    """
    if raw_status is None:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(raw_status)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(raw_status)
    return ParcelStatus.UNKNOWN


def map_event_status(raw_status: str | None) -> ParcelStatus | None:
    """Map a history event's raw status to a canonical status, or ``None``.

    Unmapped non-null statuses keep ``status: null`` on the history entry and
    warn once (reusing the parcel-status one-shot set).
    """
    if raw_status is None:
        return None
    mapped = _STATUS_MAP.get(raw_status)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(raw_status)
    return None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing
    on a mixed set.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_history(
    raw: dict | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the Trunkrs payload.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, sorted oldest → newest and capped to ``max_events``.

    Source is ``deliveryAttempts[]`` (``{stateName, setAt, reasonCode}``),
    which is a clean list of state transitions and reuses the same status map
    as the parcel itself. The payload also carries a richer ``auditLogs[]``,
    but that is internal ops text and every entry identifies a **driver**
    (``userSub``), so it is deliberately not surfaced — see
    ``docs/api/tracing_details.md``.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for attempt in (raw or {}).get("deliveryAttempts") or []:
        if not isinstance(attempt, dict):
            continue
        timestamp = attempt.get("setAt")
        if not timestamp:
            continue
        state_name = attempt.get("stateName")
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(state_name),
            "raw_status": state_name,
        }
        dt = _parse_iso(timestamp)
        if dt is None:
            unparseable.append(entry)
        else:
            parseable.append((dt, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def _tracking_url() -> str:
    """Return the consumer tracking page link for a parcel.

    Trunkrs has no confirmed deep-link format, so this is the plain tracking
    page — revisit if one is discovered.
    """
    return TRACKING_URL


def normalize_parcel(
    raw: dict, *, trunkrs_nr: str, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict with the raw payload under ``raw``.

    Publishes exactly the canonical key set the rest of the suite uses, so the
    aggregator and cross-carrier dashboards can read Trunkrs the same way as
    every other carrier. Field mapping is documented in
    ``docs/api/tracing_details.md``.

    Notes on the two non-obvious choices:

    * **Delivery window.** ``timeSlot`` carries both a wide promised slot
      (``low``/``high``) and a narrow live prediction (``from``/``to``). We
      prefer the narrow one and fall back to the wide one, because the narrow
      window is what the tracking page shows but is only populated once the
      tour is planned. Both are cleared once the parcel is delivered, matching
      the other suite carriers.
    * **Pickup.** Trunkrs is a home-delivery courier and the payload carries no
      pickup/ServicePoint block, so ``pickup`` is always ``False``. The
      ``shipmentFeatures``/``leaveBehindRemark`` fields describe delivery
      *preferences* (mailbox, leave with neighbour), not a pickup location, so
      they deliberately do not set it.
    """
    state = raw.get("currentState") or {}
    raw_status = state.get("stateName")
    delivered = raw_status == STATE_DELIVERED

    time_slot = raw.get("timeSlot") or {}
    planned_from = time_slot.get("from") or time_slot.get("low")
    planned_to = time_slot.get("to") or time_slot.get("high")

    return {
        "carrier": "Trunkrs",
        # The number the user entered, NOT ``raw["trunkrsNr"]``. They are the
        # same in practice (it is half of the credential pair), but the entered
        # value is the only one guaranteed to be stable: it exists before the
        # first successful poll, so deriving the barcode from the payload would
        # change a parcel's unique_id the moment data arrives — churning its
        # sensor and losing its history. The carrier's own value stays on
        # ``raw`` for reference.
        "barcode": trunkrs_nr,
        "sender": raw.get("senderName") or raw.get("merchantName"),
        "receiver": raw.get("recipientName"),
        "status": map_parcel_status(raw_status),
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": state.get("setAt") if delivered else None,
        "planned_from": None if delivered else planned_from,
        "planned_to": None if delivered else planned_to,
        "pickup": False,
        "pickup_point": None,
        "url": _tracking_url(),
        # Trunkrs does not expose weight/dimensions on the consumer endpoint;
        # kept on the shape for parity with DPD/PostNL so the aggregator can
        # read every carrier the same way.
        "weight": None,
        "dimensions": None,
        "history": build_history(raw) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalized parcels sorted by the ISO timestamp at ``key_field``.

    Parcels whose value is missing or unparseable always sort to the end,
    regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        dt = _parse_iso(parcel.get(key_field))
        if dt is None:
            without_ts.append(parcel)
        else:
            with_ts.append((dt, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [p for _, p in with_ts] + without_ts


class TrunkrsCoordinator(DataUpdateCoordinator[list[dict]]):
    """Coordinator that polls each tracked Trunkrs parcel on a fixed schedule.

    Trunkrs has no account/feed, so the tracked parcels are the ``trunkrs_nr``
    + ``postal_code`` pairs the user entered (stored in the entry options).
    Each is fetched individually and merged into one list;
    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest.
    """

    def __init__(
        self, hass: HomeAssistant, client: TrunkrsApiClient, entry: ConfigEntry
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self.delivered: list[dict] = []
        # trunkrs_nr -> last successful raw payload, so a transient fetch
        # failure keeps the parcel visible instead of dropping its sensor.
        # Lives for the integration's lifetime (resets on restart).
        self._raw_cache: dict[str, dict] = {}
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this Trunkrs device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _tracked(self) -> list[dict]:
        """Return the configured ``{trunkrs_nr, postal_code}`` pairs."""
        return list(self.config_entry.options.get(CONF_PARCELS, []))

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(
                CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
            )
        )

    def _apply_delivered_filter(self, parcels: list[dict]) -> list[dict]:
        """Trim the delivered list per the configured retention option.

        ``parcels`` is already sorted newest-first. ``days`` keeps deliveries
        from the last N days (an unparseable ``delivered_at`` is kept); the
        ``parcels`` type keeps the N most recent. The parcels stay *tracked*
        either way — this only controls what the delivered sensor shows.
        """
        options = self.config_entry.options
        filter_type = options.get(
            CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
        )
        amount = int(
            options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
        )
        if filter_type == "days":
            cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
            return [
                p
                for p in parcels
                if (dt := _parse_iso(p.get("delivered_at"))) is None or dt >= cutoff
            ]
        return parcels[:amount]

    async def _async_update_data(self) -> list[dict]:
        tracked = self._tracked()
        pairs = [
            (item[CONF_TRUNKRS_NR], item[CONF_POSTAL_CODE])
            for item in tracked
            if item.get(CONF_TRUNKRS_NR) and item.get(CONF_POSTAL_CODE)
        ]

        # Drop cache entries for parcels that were untracked, so the cache
        # stays bounded to what the user still follows.
        tracked_numbers = {trunkrs_nr for trunkrs_nr, _ in pairs}
        self._raw_cache = {
            k: v for k, v in self._raw_cache.items() if k in tracked_numbers
        }

        results = await asyncio.gather(
            *(
                self._client.async_get_parcel(trunkrs_nr, postal_code)
                for trunkrs_nr, postal_code in pairs
            ),
            return_exceptions=True,
        )

        entries: list[tuple[str, dict]] = []
        errors = 0
        for (trunkrs_nr, _), result in zip(pairs, results):
            if isinstance(result, BaseException):
                if not isinstance(result, (TrunkrsApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                # An auth error is the user's problem (wrong number/postcode),
                # not an outage — say so plainly instead of "fetch failed".
                if isinstance(result, TrunkrsAuthError):
                    _LOGGER.warning(
                        "Trunkrs rejected %s — check the number and postcode",
                        trunkrs_nr,
                    )
                else:
                    _LOGGER.warning(
                        "Trunkrs fetch failed for %s: %s", trunkrs_nr, result
                    )
                cached = self._raw_cache.get(trunkrs_nr)
                if cached is not None:
                    entries.append((trunkrs_nr, cached))
                continue

            if result is None:
                # Empty body — keep prior data if we have it, otherwise show a
                # pending placeholder so the user still sees the tracked parcel.
                entries.append((trunkrs_nr, self._raw_cache.get(trunkrs_nr) or {}))
                continue

            self._raw_cache[trunkrs_nr] = result
            entries.append((trunkrs_nr, result))

        if pairs and errors == len(pairs) and not entries:
            raise UpdateFailed("Trunkrs unreachable for all tracked parcels")

        include_history = self._include_history
        normalized = [
            normalize_parcel(raw, trunkrs_nr=trunkrs_nr, include_history=include_history)
            for trunkrs_nr, raw in entries
        ]
        active = [p for p in normalized if not p["delivered"]]
        delivered = [p for p in normalized if p["delivered"]]

        self.delivered = self._apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True)
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set (mirrors the other suite carriers).
        incoming = normalized_active + self.delivered
        self._fire_change_events(incoming)
        self._known_state = {
            p["barcode"]: p["status"] for p in incoming if p.get("barcode")
        }
        self._known_delivery_times = {
            p["barcode"]: (p.get("planned_from"), p.get("planned_to"))
            for p in incoming
            if p.get("barcode")
        }

        # Only stamp the diagnostic timestamp when at least one fetch actually
        # succeeded (or nothing is tracked) — a poll that was served entirely
        # from cache must not present itself as a successful update.
        if not pairs or errors < len(pairs):
            self.last_success_time = datetime.now(timezone.utc)
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new vs. already present before HA started. Mirrors the other
        suite carriers, including the ``device_id`` on every payload and the
        ``value → null`` ETA transitions staying intentionally silent. The
        parcels span active + delivered, so the terminal hop is visible: a
        change **to** ``DELIVERED`` fires only ``trunkrs_parcel_delivered``
        (never also ``_status_changed``), a barcode first seen
        already-delivered fires nothing, and ``registered`` only fires for
        not-yet-delivered new barcodes.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )
