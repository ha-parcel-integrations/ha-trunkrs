"""Services for the Trunkrs parcel tracker integration.

``trunkrs.track_parcel`` / ``trunkrs.untrack_parcel`` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button,
or an automation that reads tracking numbers out of e-mail, can start tracking
a parcel straight away.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .config_flow import (
    async_verify_parcel,
    normalize_postcode,
    normalize_trunkrs_nr,
    valid_postcode,
    valid_trunkrs_nr,
)
from .const import (
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

# ``tracking_code`` is the standard field across every parcel-suite carrier.
# ``trunkrs_nr`` is a deprecated alias kept working for backwards compatibility
# and will be removed in a future release; both are optional at the schema
# level and ``_resolve_code`` requires exactly one.
_TRACK_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_TRUNKRS_NR): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_TRUNKRS_NR): cv.string,
    }
)

# One-shot so the deprecation is logged once per HA session, not per call.
_trunkrs_nr_deprecation_logged = False


def _resolve_code(call: ServiceCall) -> str:
    """Return the tracking code from a service call.

    Accepts the standard ``tracking_code`` field or the deprecated
    ``trunkrs_nr`` alias. Using ``trunkrs_nr`` logs a one-shot deprecation
    warning; passing neither raises.
    """
    global _trunkrs_nr_deprecation_logged
    code = call.data.get(CONF_TRACKING_CODE)
    if code is None:
        code = call.data.get(CONF_TRUNKRS_NR)
        if code is not None and not _trunkrs_nr_deprecation_logged:
            _trunkrs_nr_deprecation_logged = True
            _LOGGER.warning(
                "The '%s' field of the Trunkrs %s service is deprecated and "
                "will be removed in a future release — use '%s' instead.",
                CONF_TRUNKRS_NR,
                call.service,
                CONF_TRACKING_CODE,
            )
    if code is None:
        raise ServiceValidationError(f"'{CONF_TRACKING_CODE}' is required")
    return code


def _resolve_entry(hass: HomeAssistant, postal_code: str | None):
    """Pick the Trunkrs hub to act on.

    With one hub, that hub. With several, the ``postal_code`` argument selects
    it; if omitted and ambiguous, raise so the caller knows to specify one.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Trunkrs is not set up")
    if postal_code:
        target = normalize_postcode(postal_code)
        for entry in entries:
            if entry.options.get(CONF_POSTAL_CODE) == target:
                return entry
        raise ServiceValidationError(f"No Trunkrs hub for postal code {target}")
    if len(entries) == 1:
        return entries[0]
    raise ServiceValidationError(
        "Multiple Trunkrs hubs are set up — pass postal_code to choose one"
    )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Trunkrs services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        trunkrs_nr = normalize_trunkrs_nr(_resolve_code(call))
        if not valid_trunkrs_nr(trunkrs_nr):
            raise ServiceValidationError(
                f"'{trunkrs_nr}' is not a valid Trunkrs number"
            )
        entry = _resolve_entry(hass, call.data.get(CONF_POSTAL_CODE))
        postal_code = normalize_postcode(
            call.data.get(CONF_POSTAL_CODE) or entry.options.get(CONF_POSTAL_CODE, "")
        )
        if not valid_postcode(postal_code):
            raise ServiceValidationError(f"'{postal_code}' is not a valid postal code")

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRUNKRS_NR] == trunkrs_nr for p in parcels):
            return  # already tracked — no-op

        # Trunkrs can tell us authoritatively whether the pair exists. Only a
        # definite "no" blocks the call; an unreachable API returns None and we
        # accept the parcel anyway rather than failing on an outage.
        if (await async_verify_parcel(hass, trunkrs_nr, postal_code)) is False:
            raise ServiceValidationError(
                f"Trunkrs does not know parcel '{trunkrs_nr}' for postal code "
                f"'{postal_code}'"
            )

        parcels.append({CONF_TRUNKRS_NR: trunkrs_nr, CONF_POSTAL_CODE: postal_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        trunkrs_nr = normalize_trunkrs_nr(_resolve_code(call))
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError("Trunkrs is not set up")
        # Remove the parcel from whichever hub(s) track it.
        for entry in entries:
            current = entry.options.get(CONF_PARCELS, [])
            kept = [p for p in current if p[CONF_TRUNKRS_NR] != trunkrs_nr]
            if len(kept) != len(current):
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PARCELS: kept}
                )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Trunkrs services once the last hub is gone."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
