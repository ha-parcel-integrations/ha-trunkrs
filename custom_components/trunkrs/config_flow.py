"""Config flow for the Trunkrs parcel tracker integration."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TrunkrsApiClient, TrunkrsApiError
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
    REFRESH_INTERVAL_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

# Trunkrs delivers in the Netherlands, so the hub postcode is validated as an
# NL one (``1234AB``). Broaden this when Trunkrs coverage in another country
# is confirmed to work against the same endpoint.
_POSTCODE_RE = re.compile(r"^\d{4}[A-Z]{2}$")

# The exact Trunkrs-number format is not documented and we have not seen
# enough real numbers to pin it down, so this is deliberately permissive —
# the real check is the API's own ``/tracing/verify`` call, which tells us
# authoritatively whether a number + postcode pair exists.
_TRUNKRS_NR_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{3,29}$")

_HUB_SCHEMA = vol.Schema({vol.Required(CONF_POSTAL_CODE): str})


def normalize_postcode(value: str) -> str:
    """Return the postcode without spaces and upper-cased (``1234AB``)."""
    return value.replace(" ", "").upper()


def normalize_trunkrs_nr(value: str) -> str:
    """Return the Trunkrs number trimmed and upper-cased."""
    return value.strip().upper()


def valid_trunkrs_nr(value: str) -> bool:
    """Whether ``value`` is shaped like a Trunkrs number (cheap pre-check)."""
    return bool(_TRUNKRS_NR_RE.match(value))


def valid_postcode(value: str) -> bool:
    """Whether ``value`` is a valid Dutch postcode."""
    return bool(_POSTCODE_RE.match(value))


async def async_verify_parcel(
    hass, trunkrs_nr: str, postal_code: str
) -> bool | None:
    """Verify a number/postcode pair with Trunkrs.

    Returns ``True`` when Trunkrs knows the pair, ``False`` when it rejects it,
    and ``None`` when we could not reach Trunkrs at all. ``None`` is treated as
    "accept anyway" by the callers: a service outage must not stop a user from
    adding a parcel they know is valid.
    """
    client = TrunkrsApiClient(async_get_clientsession(hass))
    try:
        return await client.async_verify(trunkrs_nr, postal_code)
    except (TrunkrsApiError, aiohttp.ClientError) as err:
        _LOGGER.warning("Could not verify %s with Trunkrs: %s", trunkrs_nr, err)
        return None


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _interval_selector() -> selector.SelectSelector:
    """The refresh-interval dropdown selector (options translated via strings)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[str(m) for m in REFRESH_INTERVAL_OPTIONS],
            translation_key=CONF_REFRESH_INTERVAL,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class TrunkrsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Trunkrs integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TrunkrsOptionsFlowHandler:
        """Return the options flow handler."""
        return TrunkrsOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a Trunkrs hub — one per delivery postal code.

        Multiple hubs are allowed (e.g. home + work); each is keyed on its
        postal code, so the same postcode can only be added once. The postcode
        becomes the hub default, so adding a parcel later needs only its
        Trunkrs number — the postcode is half of the API credential pair.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            postal_code = normalize_postcode(user_input[CONF_POSTAL_CODE])
            if not valid_postcode(postal_code):
                errors[CONF_POSTAL_CODE] = "invalid_postcode"
            else:
                await self.async_set_unique_id(postal_code)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Trunkrs ({postal_code})",
                    data={},
                    options={
                        CONF_PARCELS: [],
                        CONF_POSTAL_CODE: postal_code,
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_REFRESH_INTERVAL: DEFAULT_REFRESH_INTERVAL,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="user", data_schema=_HUB_SCHEMA, errors=errors
        )


class TrunkrsOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels, history and polling in one sectioned form.

    Mirrors the other suite carriers' section layout (here: ``parcels`` /
    ``delivered`` / ``history`` / ``polling``). Adding a parcel needs only its
    number — the postcode is inherited from the hub. Changes apply live via
    HA's options-update listener (which refreshes the coordinator), so
    new/removed per-parcel sensors appear and disappear immediately.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the single sectioned options form."""
        errors: dict[str, str] = {}
        parcels = _current_parcels(self.config_entry)
        hub_postcode = self.config_entry.options.get(CONF_POSTAL_CODE, "")

        if user_input is not None:
            parcels_section = user_input.get("parcels", {})
            delivered_section = user_input.get("delivered", {})
            history_section = user_input.get("history", {})
            polling_section = user_input.get("polling", {})

            # Remove first, then add — so re-adding a just-removed number works.
            to_remove = set(parcels_section.get("remove", []))
            parcels = [p for p in parcels if p[CONF_TRUNKRS_NR] not in to_remove]

            add_nr = normalize_trunkrs_nr(parcels_section.get("add") or "")
            if add_nr:
                if not valid_trunkrs_nr(add_nr):
                    errors["base"] = "invalid_trunkrs_nr"
                elif any(p[CONF_TRUNKRS_NR] == add_nr for p in parcels):
                    errors["base"] = "already_tracked"
                elif (
                    await async_verify_parcel(self.hass, add_nr, hub_postcode)
                ) is False:
                    # Trunkrs answered, and it does not know this pair.
                    errors["base"] = "unknown_parcel"
                else:
                    parcels.append(
                        {CONF_TRUNKRS_NR: add_nr, CONF_POSTAL_CODE: hub_postcode}
                    )

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_POSTAL_CODE: hub_postcode,
                        CONF_PARCELS: parcels,
                        CONF_DELIVERED_FILTER_TYPE: delivered_section[
                            CONF_DELIVERED_FILTER_TYPE
                        ],
                        CONF_DELIVERED_FILTER_AMOUNT: int(
                            delivered_section[CONF_DELIVERED_FILTER_AMOUNT]
                        ),
                        CONF_INCLUDE_HISTORY: bool(
                            history_section[CONF_INCLUDE_HISTORY]
                        ),
                        CONF_REFRESH_INTERVAL: int(
                            polling_section[CONF_REFRESH_INTERVAL]
                        ),
                    },
                )

        current = self.config_entry.options

        parcels_fields: dict[Any, Any] = {vol.Optional("add", default=""): str}
        if parcels:
            parcels_fields[vol.Optional("remove", default=[])] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=p[CONF_TRUNKRS_NR],
                            label=f"{p[CONF_TRUNKRS_NR]} ({p[CONF_POSTAL_CODE]})",
                        )
                        for p in parcels
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )

        schema = vol.Schema(
            {
                vol.Required("parcels"): section(
                    vol.Schema(parcels_fields), {"collapsed": False}
                ),
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=["days", "parcels"],
                                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                            vol.Required(
                                CONF_DELIVERED_FILTER_AMOUNT,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    DEFAULT_DELIVERED_FILTER_AMOUNT,
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1,
                                    max=365,
                                    step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required("polling"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_REFRESH_INTERVAL,
                                # str(): selector option values are strings, so a
                                # stored int default trips "expected str" on submit.
                                default=str(
                                    current.get(
                                        CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
                                    )
                                ),
                            ): _interval_selector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
