"""Diagnostics support for the Trunkrs parcel tracker integration.

Doubles as the mechanism for finishing this integration: the raw
``/tracing/details`` payload is preserved on every parcel under ``raw``, so a
user who downloads diagnostics and attaches it to a GitHub issue gives us
exactly what is needed to write the field mapping (see ``coordinator.py``).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TrunkrsConfigEntry
from .const import NEW_ISSUE_URL

# Keyed on the real ``/tracing/details`` field names (documented in
# docs/api/tracing_details.md). ``async_redact_data`` matches keys recursively,
# so naming a container like ``recipientLocation`` redacts the whole block.
#
# Note the two non-obvious entries: the payload can carry a **neighbour's**
# name/address when a parcel was left next door (third-party PII), and both
# ``auditLogs`` and ``tourDetails`` identify the **driver** — ``userSub``,
# ``driverId``, plus ``polyline``, which is an encoded trace of the driver's
# route. None of that belongs in a shared diagnostics file.
TO_REDACT = {
    # our own stored credentials — the number+postcode pair IS the API key
    "trunkrs_nr",
    "postal_code",
    "parcels",
    # identifiers
    "barcode",
    "trunkrsNr",
    "shipmentId",
    "recipient_id",
    # people and shops
    "recipientName",
    "senderName",
    "merchantName",
    "sender",
    "receiver",
    # third-party PII: a neighbour who accepted the parcel
    "neighbourName",
    "neighbourAddressLine",
    # free text written by the recipient (e.g. where to leave the parcel)
    "remark",
    "leaveBehindRemark",
    # the full delivery address, incl. coordinates
    "recipientLocation",
    # driver identity and route trace
    "auditLogs",
    "userSub",
    "driverId",
    "polyline",
    # generic fallbacks, in case a field we have not seen shows up
    "address",
    "city",
    "postalCode",
    "postcode",
    "latitude",
    "longitude",
    "email",
    "phone",
    "phoneNumber",
}

_SHARING_NOTE = (
    "The Trunkrs field mapping is implemented, but the status vocabulary is "
    "still incomplete: only 'SHIPMENT_DELIVERED' is confirmed, so a parcel in "
    "another state reports 'unknown' and logs a one-shot warning. If you see "
    f"such a warning, sharing that log line at {NEW_ISSUE_URL} lets us map the "
    "remaining statuses. Personal fields are redacted below, but please still "
    "read through the output before posting it publicly."
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrunkrsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Trunkrs config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "note": _SHARING_NOTE,
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
