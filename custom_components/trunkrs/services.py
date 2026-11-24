"""Services for the Trunkrs parcel tracker integration.

``trunkrs.track_parcel`` / ``trunkrs.untrack_parcel`` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button,
or an automation that reads tracking numbers out of e-mail, can start tracking
a parcel straight away. ``trunkrs.update_contents`` sets or clears the text
describing what is in a parcel, tracked or not, on the hub(s) that list the
code.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import (
    async_verify_parcel,
    normalize_postcode,
    normalize_trunkrs_nr,
    valid_postcode,
    valid_trunkrs_nr,
)
from .const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
)
from .contents import ContentsStore, normalize_code

_LOGGER = logging.getLogger(__name__)

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

# ``tracking_code`` is the standard field across every parcel-suite carrier.
_TRACK_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TRACKING_CODE): cv.string,
    }
)
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


def _resolve_code(call: ServiceCall) -> str:
    """Return the tracking code from a service call, or raise if absent."""
    code = call.data.get(CONF_TRACKING_CODE)
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


def _loaded_coordinator(entry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("Trunkrs is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


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

        coordinator = _loaded_coordinator(entry) if CONF_CONTENTS in call.data else None
        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRUNKRS_NR] == trunkrs_nr for p in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                trunkrs_nr, call.data[CONF_CONTENTS]
            ):
                await coordinator.async_request_refresh()
            return

        # Trunkrs can tell us authoritatively whether the pair exists. Only a
        # definite "no" blocks the call; an unreachable API returns None and we
        # accept the parcel anyway rather than failing on an outage.
        if (await async_verify_parcel(hass, trunkrs_nr, postal_code)) is False:
            raise ServiceValidationError(
                f"Trunkrs does not know parcel '{trunkrs_nr}' for postal code "
                f"'{postal_code}'"
            )

        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(trunkrs_nr, call.data[CONF_CONTENTS])
        parcels.append({CONF_TRUNKRS_NR: trunkrs_nr})
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
                if (store := _contents_store(entry)) is not None:
                    store.async_set(trunkrs_nr, None)
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PARCELS: kept}
                )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("Trunkrs is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(
                    f"'{entry_id}' is not a loaded Trunkrs entry"
                )
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two hubs, so every hub that lists it gets the
            # value; guessing a hub would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_CONTENTS,
        _update_contents,
        schema=_UPDATE_CONTENTS_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Trunkrs services once the last hub is gone."""
    for service in (
        SERVICE_TRACK_PARCEL,
        SERVICE_UNTRACK_PARCEL,
        SERVICE_UPDATE_CONTENTS,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
