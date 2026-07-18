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

# ⚠️ We do not yet know the field names Trunkrs uses in ``/tracing/details``,
# so precise redaction is impossible. This list is deliberately broad and
# covers the common casings of anything that could identify a person or an
# address, across the spellings carriers typically use. It is applied
# recursively by ``async_redact_data``.
#
# Because the payload shape is unknown, the diagnostics output ALSO carries an
# explicit warning telling the user to eyeball it before sharing — see
# ``_SHARING_NOTE``. Revisit this set (and tighten it) once the payload is
# documented.
TO_REDACT = {
    # our own stored credentials — the number+postcode pair IS the API key
    "trunkrs_nr",
    "postal_code",
    "parcels",
    # identifiers
    "barcode",
    "trunkrsNr",
    "trunkrsNumber",
    "shipmentReference",
    "orderReference",
    "reference",
    "uuid",
    "id",
    # people
    "name",
    "firstName",
    "lastName",
    "fullName",
    "contactName",
    "recipient",
    "receiver",
    "sender",
    "email",
    "emailAddress",
    "phone",
    "phoneNumber",
    "mobile",
    # addresses
    "address",
    "addressLine1",
    "addressLine2",
    "street",
    "streetName",
    "houseNumber",
    "houseNumberAddition",
    "city",
    "postalCode",
    "postcode",
    "zipCode",
    "zipcode",
    "country",
    "latitude",
    "longitude",
    "lat",
    "lng",
    "coordinates",
}

_SHARING_NOTE = (
    "This integration cannot map Trunkrs parcel data yet — the response shape "
    "of GET /tracing/details is unknown, so every parcel reports 'unknown'. "
    "The untouched payload is preserved below under incoming[].raw. Sharing it "
    f"at {NEW_ISSUE_URL} is all that is needed to finish the integration. "
    "IMPORTANT: because the field names are unknown, redaction here is "
    "best-effort on commonly used key names — please read through the output "
    "and remove anything personal before posting it publicly."
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
