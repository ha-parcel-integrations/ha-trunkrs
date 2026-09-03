"""Pure parcel-mapping helpers for the Trunkrs integration (no I/O).

Keeping the carrier-specific mapping — the status map, the history builder, the
canonical :func:`normalize_parcel`, the sort contract and the delivered filter —
out of ``coordinator.py`` lets it be unit-tested as plain functions and mirrors
the layout every other carrier in the suite uses. The coordinator only fetches,
caches and fires events.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
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
# ⚠️ The status vocabulary is still INCOMPLETE. Only the values below have
# actually been observed. The ``SHIPMENT_*`` prefix suggests a family, but the
# other values are deliberately NOT guessed: a wrong guess would silently
# report the wrong status, whereas an unmapped one reports ``unknown`` and logs
# a one-shot warning with a copy-paste issue link — so the rest of the
# vocabulary collects itself from real users. Add each confirmed value here.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "SHIPMENT_DELIVERED": ParcelStatus.DELIVERED,
    # Confirmed in issue #5: parcel at the sort facility, not yet on a
    # delivery vehicle — still "in the carrier's network".
    "SHIPMENT_SORTED": ParcelStatus.IN_TRANSIT,
    # Confirmed in issue #6: the delivery driver has taken the parcel onto
    # their route ("Onderweg" / "almost there" per the reporter) — this is
    # today's delivery vehicle, not just the carrier's network.
    "SHIPMENT_ACCEPTED_BY_DRIVER": ParcelStatus.OUT_FOR_DELIVERY,
    # Confirmed in issue #6: sorted at a sub-depot ("Gesorteerd") is the same
    # network stage as SHIPMENT_SORTED, just a different facility.
    "SHIPMENT_SORTED_AT_SUB_DEPOT": ParcelStatus.IN_TRANSIT,
}

# The one state we can also act on structurally (``delivered`` / ``delivered_at``
# / clearing the ETA window), kept as a constant so the test and the mapping
# cannot drift apart.
STATE_DELIVERED = "SHIPMENT_DELIVERED"

# Raw statuses we have already warned about, so each unmapped one is logged
# only once per HA session.
_unmapped_statuses_logged: set[str] = set()


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


# The failed / not-delivered vocabulary is unconfirmed — only SHIPMENT_DELIVERED
# has been observed, and the payload's ``reasonCode`` is presumably populated for
# a failed attempt (see TODO.md). We have never seen one, so the first
# ``reasonCode`` we encounter — on the current state or a delivery attempt — is
# logged once so a tester can report the failed-state vocabulary. A reasonCode is
# a code (e.g. NOBODY_HOME), not personal data. See NEW_ISSUE_URL.
_reason_code_logged = False


def _note_reason_code(raw: dict) -> None:
    """One-shot: report a delivery reasonCode we have not mapped yet."""
    global _reason_code_logged
    if _reason_code_logged:
        return
    sources = [raw.get("currentState") or {}]
    sources += [a for a in (raw.get("deliveryAttempts") or []) if isinstance(a, dict)]
    seen = sorted(
        {
            f"{s.get('stateName')}:{s.get('reasonCode')}"
            for s in sources
            if isinstance(s, dict) and s.get("reasonCode")
        }
    )
    if not seen:
        return
    _reason_code_logged = True
    _LOGGER.warning(
        "Trunkrs reported a delivery reasonCode we have not mapped yet "
        "(state:reason=%s) — this is the failed/not-delivered vocabulary we "
        "still need. Please report it, a diagnostics file is ideal: %s",
        seen,
        NEW_ISSUE_URL,
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


def _tracking_url(trunkrs_nr: str, postal_code: str | None) -> str:
    """Return the consumer tracking page link for a parcel.

    Confirmed in issue #7: ``{trunkrs_nr}/{postal_code}`` deep-links straight
    to the parcel. Falls back to the plain tracking page when the postcode
    isn't available (e.g. called without it in a test).
    """
    if not postal_code:
        return TRACKING_URL
    return f"{TRACKING_URL}{trunkrs_nr}/{postal_code}"


def normalize_parcel(
    raw: dict,
    *,
    trunkrs_nr: str,
    postal_code: str | None = None,
    include_history: bool = False,
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
    _note_reason_code(raw)
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
        "url": _tracking_url(trunkrs_nr, postal_code),
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


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` is already sorted newest-first. ``days`` keeps deliveries from
    the last N days (an unparseable ``delivered_at`` is kept); the ``parcels``
    type keeps the N most recent. The parcels stay *tracked* either way — this
    only controls what the delivered sensor shows.
    """
    options = entry.options
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
