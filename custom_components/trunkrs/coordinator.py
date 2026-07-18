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

# ===========================================================================
# ⚠️  PAYLOAD MAPPING GAP — the one thing this integration still needs
# ===========================================================================
#
# The Trunkrs *transport* is fully reverse-engineered and live-confirmed:
# host, Basic-auth scheme and both endpoints all behave as expected (a wrong
# number/postcode pair returns 401, a right one returns 200). What we have
# NOT been able to capture is the **body** of a successful
# ``GET /tracing/details`` response, because that needs a real Trunkrs parcel.
#
# Until someone shares one, everything below that reads *fields out of* the
# payload is deliberately left unmapped rather than guessed:
#
#   * ``_STATUS_MAP``      — empty; every parcel reports ``unknown``.
#   * ``normalize_parcel`` — fills only what we know for certain (the barcode
#                            is the Trunkrs number the user typed) and leaves
#                            payload-derived fields ``None``.
#   * ``build_history``    — returns an empty list; the event array key and
#                            per-event field names are unknown.
#
# Guessing field names here would produce an integration that looks like it
# works and silently reports wrong data, which is worse than one that honestly
# reports "unknown". The full raw payload IS preserved on ``parcel["raw"]``
# and surfaced (redacted) in diagnostics, so a single user download unblocks
# all of this. See TODO.md for the exact ask.
#
# To finish the integration: fill in ``_STATUS_MAP`` and the marked sections
# of ``normalize_parcel`` / ``build_history``. Nothing else should need to
# change — the polling, filtering, sorting and event plumbing below is
# payload-independent and already complete.
# ===========================================================================

# Trunkrs raw status value -> canonical ParcelStatus.
# EMPTY UNTIL A REAL PAYLOAD IS AVAILABLE — see the block above.
_STATUS_MAP: dict[str, ParcelStatus] = {}

# Raw statuses we have already warned about, so each unmapped one is logged
# only once per HA session.
_unmapped_statuses_logged: set[str] = set()
# One-shot flag for the "we have a payload but no mapping yet" notice.
_payload_shape_logged = False


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


def _log_payload_shape(raw: dict) -> None:
    """Log the payload's top-level keys once, to help finish the mapping.

    This is the cheapest way for a user to tell us what ``/tracing/details``
    actually returns without sending the whole (personal) body: the key names
    alone are enough to write the field mapping.
    """
    global _payload_shape_logged
    if _payload_shape_logged or not raw:
        return
    _payload_shape_logged = True
    _LOGGER.warning(
        "Trunkrs parcel data is not mapped yet, so parcels will show as "
        "'unknown'. Please help finish this integration by opening an issue "
        "at %s with the payload keys below (or, better, by attaching the "
        "integration's redacted diagnostics download).\n  top-level keys: %s",
        NEW_ISSUE_URL,
        sorted(raw.keys()),
    )


def map_parcel_status(raw_status: str | None) -> ParcelStatus:
    """Map a raw Trunkrs status to a canonical :class:`ParcelStatus`.

    ``None`` reports ``unknown`` silently; an unmapped non-null status reports
    ``unknown`` with a one-shot warning. While ``_STATUS_MAP`` is empty this
    always returns ``unknown`` — see the mapping-gap block at the top.
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

    ⚠️ NOT IMPLEMENTED: the key holding the event array, and the per-event
    timestamp/status field names, are unknown until a real payload is
    available (see the mapping-gap block at the top). Returns ``[]`` for now
    rather than guessing. When the payload is known, iterate the event array
    and reuse :func:`map_event_status` + :func:`_parse_iso` exactly as the
    other suite carriers do.
    """
    return []


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
    every other carrier.

    ⚠️ PARTIALLY IMPLEMENTED — see the mapping-gap block at the top of this
    module. Only the fields we can know without the payload are populated:

      * ``carrier``  — constant.
      * ``barcode``  — the Trunkrs number the user entered (we always know it,
                       it is half of the credential pair).
      * ``url``      — the consumer tracking page.
      * ``raw``      — the untouched payload, which is what we need shared.

    Everything else is ``None`` / ``unknown`` on purpose: guessing field names
    would silently report wrong data. ``delivered`` stays ``False`` so no
    parcel is ever wrongly filed away as completed.
    """
    _log_payload_shape(raw)

    # --- TODO(payload): map these from `raw` once a real response is known ---
    # Suggested order of work, mirroring the other carriers:
    #   raw_status   = raw.get(<status field>)
    #   delivered    = <terminal status test>
    #   delivered_at = raw.get(<delivered timestamp>)
    #   planned_from / planned_to = <ETA window>
    #   sender / receiver         = <party names>
    #   pickup / pickup_point     = <ServicePoint block>
    raw_status: str | None = None
    delivered = False
    delivered_at: str | None = None
    planned_from: str | None = None
    planned_to: str | None = None
    sender: str | None = None
    receiver: str | None = None
    pickup = False
    pickup_point: str | None = None
    # ------------------------------------------------------------------------

    return {
        "carrier": "Trunkrs",
        "barcode": trunkrs_nr,
        "sender": sender,
        "receiver": receiver,
        "status": map_parcel_status(raw_status),
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": _tracking_url(),
        # Trunkrs does not expose weight/dimensions on the consumer endpoint as
        # far as we know; kept on the shape for parity with DPD/PostNL so the
        # aggregator can read every carrier the same way.
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
