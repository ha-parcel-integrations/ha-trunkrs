"""Trunkrs parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TrunkrsApiClient
from .const import DOMAIN, PLATFORMS
from .coordinator import TrunkrsCoordinator, _refresh_interval
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)


@dataclass
class TrunkrsData:
    """Runtime data attached to a Trunkrs config entry."""

    client: TrunkrsApiClient
    coordinator: TrunkrsCoordinator


type TrunkrsConfigEntry = ConfigEntry[TrunkrsData]


async def async_setup_entry(hass: HomeAssistant, entry: TrunkrsConfigEntry) -> bool:
    """Set up Trunkrs from a config entry."""
    # Credentials are per parcel (Basic auth built per request), so there is no
    # shared login and the HA-managed session is fine — no private cookie jar
    # needed, unlike the account-based carriers in the suite.
    client = TrunkrsApiClient(async_get_clientsession(hass))
    coordinator = TrunkrsCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = TrunkrsData(client=client, coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, interval, history) live via
    # a coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. The update listener does NOT reload, so it does
    # not trip the config-entry-listener deprecation.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: TrunkrsConfigEntry
) -> None:
    """Apply changed options: retune the interval and refresh the coordinator."""
    coordinator = entry.runtime_data.coordinator
    coordinator.update_interval = _refresh_interval(entry)
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: TrunkrsConfigEntry) -> bool:
    """Unload a Trunkrs config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # The services are shared across hubs, so only remove them once the last
    # hub is gone — otherwise unloading one hub would break the others.
    others_loaded = any(
        other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
        for other in hass.config_entries.async_entries(DOMAIN)
    )
    if not others_loaded:
        async_unload_services(hass)
    return True
