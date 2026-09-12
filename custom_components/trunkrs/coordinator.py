"""Coordinator for the Trunkrs parcel tracker integration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TrunkrsApiClient, TrunkrsApiError, TrunkrsAuthError
from .const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .parcels import apply_delivered_filter, normalize_parcel, sort_parcels_by_ts

_LOGGER = logging.getLogger(__name__)


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the barcode-based model (Section 2.1).

    ``None`` means "stop polling entirely" — nothing is tracked, or every
    tracked parcel is already delivered (already filtered out of
    ``active_parcels`` by the caller).
    """
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


def _next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``.

    ``None`` fully suspends scheduling (``DataUpdateCoordinator`` honours
    this natively). Otherwise, clamp the naive next-due time forward to the
    next anchor whenever it would land inside the quiet window — including
    when ``now`` itself is already inside it (an anchor poll computing its
    own follow-up).
    """
    if tier_minutes is None:
        return None

    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


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
            # Recomputed at the end of every refresh — start with the hot
            # cadence so the very first poll, right after setup, happens
            # promptly regardless of what it finds.
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        # trunkrs_nr -> last successful raw payload, so a transient fetch
        # failure keeps the parcel visible instead of dropping its sensor.
        # Lives for the integration's lifetime (resets on restart).
        self._raw_cache: dict[str, dict] = {}
        # trunkrs_nr values confirmed delivered on a prior refresh — excluded
        # from the fetch this cycle since a delivered parcel's payload can
        # never change again. Keyed on trunkrs_nr (not the barcode — a
        # never-scanned parcel has no barcode). Lives for the integration's
        # lifetime (resets on restart).
        self._delivered_codes: set[str] = set()
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this Trunkrs device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        # Last tier computed by _hottest_tier_minutes — surfaced in
        # diagnostics; ``None`` before the first refresh and whenever polling
        # is fully suspended (nothing tracked, or everything delivered).
        self._current_tier_minutes: int | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """trunkrs_nr values currently skipped from the fetch (diagnostics only)."""
        return self._delivered_codes

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
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
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def _apply_delivered_filter(self, parcels: list[dict]) -> list[dict]:
        """Trim the delivered list per the configured retention option.

        Thin wrapper around :func:`.parcels.apply_delivered_filter` so the
        coordinator and the tests can call it with just the parcels.
        """
        return apply_delivered_filter(parcels, self.config_entry)

    async def _async_update_data(self) -> list[dict]:
        tracked = self._tracked()
        postal_code = self.config_entry.options.get(CONF_POSTAL_CODE)
        pairs = [
            (item[CONF_TRUNKRS_NR], postal_code)
            for item in tracked
            if item.get(CONF_TRUNKRS_NR) and postal_code
        ]

        # Drop cache entries for parcels that were untracked, so the cache
        # stays bounded to what the user still follows.
        tracked_numbers = {trunkrs_nr for trunkrs_nr, _ in pairs}
        self._raw_cache = {
            k: v for k, v in self._raw_cache.items() if k in tracked_numbers
        }
        self._delivered_codes &= tracked_numbers

        # A delivered parcel's payload can never change again, so it is
        # dropped from the fetch — not from ``pairs``/the options list, which
        # stays untouched until the user removes it by hand.
        pairs_to_fetch = [
            (trunkrs_nr, postal_code)
            for trunkrs_nr, postal_code in pairs
            if trunkrs_nr not in self._delivered_codes
        ]

        results = await asyncio.gather(
            *(
                self._client.async_get_parcel(trunkrs_nr, postal_code)
                for trunkrs_nr, postal_code in pairs_to_fetch
            ),
            return_exceptions=True,
        )

        entries_by_nr: dict[str, dict] = {}
        errors = 0
        for (trunkrs_nr, _), result in zip(pairs_to_fetch, results):
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
                    entries_by_nr[trunkrs_nr] = cached
                continue

            if result is None:
                # Empty body — keep prior data if we have it, otherwise show a
                # pending placeholder so the user still sees the tracked parcel.
                entries_by_nr[trunkrs_nr] = self._raw_cache.get(trunkrs_nr) or {}
                continue

            self._raw_cache[trunkrs_nr] = result
            entries_by_nr[trunkrs_nr] = result

        # trunkrs_nr values skipped from the fetch above (already confirmed
        # delivered) — re-add their cached payload so the delivered sensor
        # keeps its data until the retention filter drops it.
        for trunkrs_nr in self._delivered_codes:
            cached = self._raw_cache.get(trunkrs_nr)
            if cached is not None:
                entries_by_nr[trunkrs_nr] = cached

        if (
            pairs_to_fetch
            and errors == len(pairs_to_fetch)
            and not entries_by_nr
        ):
            raise UpdateFailed("Trunkrs unreachable for all tracked parcels")

        # postal_code is uniform per hub (one CONF_POSTAL_CODE per entry), so
        # any pair's second element works for entries served purely from cache.
        postal_code_by_nr = dict(pairs)
        include_history = self._include_history
        normalized_entries = [
            (
                trunkrs_nr,
                normalize_parcel(
                    raw,
                    trunkrs_nr=trunkrs_nr,
                    postal_code=postal_code_by_nr.get(trunkrs_nr, postal_code),
                    include_history=include_history,
                ),
            )
            for trunkrs_nr, raw in entries_by_nr.items()
        ]
        active = [p for _, p in normalized_entries if not p["delivered"]]
        delivered = [p for _, p in normalized_entries if p["delivered"]]
        # Rebuilt fresh from this cycle's data — a trunkrs_nr whose payload
        # just flipped to delivered is skipped starting next cycle; one that
        # somehow un-delivers (should not happen, but the fetch list must
        # never permanently drop a code) rejoins it automatically.
        self._delivered_codes = {
            trunkrs_nr for trunkrs_nr, p in normalized_entries if p["delivered"]
        }

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
        # succeeded (or nothing needed fetching) — a poll that was served
        # entirely from cache must not present itself as a successful update.
        if not pairs_to_fetch or errors < len(pairs_to_fetch):
            self.last_success_time = datetime.now(timezone.utc)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(normalized_active, now)
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
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
