"""Sensor platform for the Trunkrs parcel tracker integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TrunkrsConfigEntry
from .const import CONF_POSTAL_CODE, DOMAIN
from .coordinator import TrunkrsCoordinator

_LOGGER = logging.getLogger(__name__)

# The DataUpdateCoordinator handles fan-out to all entities; HA's per-entity
# update throttling adds nothing here.
PARALLEL_UPDATES = 0


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity for this Trunkrs hub.

    The postal code is part of the device name so multiple hubs (e.g. home and
    work) stay distinguishable — mirroring the account-in-name pattern of the
    other carriers.
    """
    postal_code = entry.options.get(CONF_POSTAL_CODE, "")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Trunkrs ({postal_code})" if postal_code else "Trunkrs",
        manufacturer="Trunkrs",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://trunkrs.nl",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrunkrsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Trunkrs sensor entities from a config entry."""
    # The coordinator is already refreshed by __init__.py before platforms are
    # forwarded, so ConfigEntryNotReady is raised from the entry setup rather
    # than (too late) from this forwarded platform.
    coordinator = entry.runtime_data.coordinator

    current_barcodes: set[str] = {
        p.get("barcode", "") for p in coordinator.data or []
    }
    entry_id = entry.entry_id

    # Remove per-parcel sensors from the registry whose barcode is no longer
    # active (e.g. the number was removed, or the parcel was delivered between
    # restarts). Scoped to the sensor domain so it never touches the refresh
    # button or the diagnostic last-update sensor.
    registry = er.async_get(hass)
    non_parcel_unique_ids = {
        f"{entry_id}_incoming_parcels",
        f"{entry_id}_next_delivery",
        f"{entry_id}_delivered_parcels",
        f"{entry_id}_last_update",
    }
    for entity_entry in er.async_entries_for_config_entry(registry, entry_id):
        if (
            entity_entry.domain == "sensor"
            and entity_entry.unique_id.startswith(f"{entry_id}_")
            and entity_entry.unique_id not in non_parcel_unique_ids
        ):
            barcode = entity_entry.unique_id[len(f"{entry_id}_"):]
            if barcode not in current_barcodes:
                registry.async_remove(entity_entry.entity_id)

    entities: list[SensorEntity] = [
        TrunkrsIncomingParcelsSensor(
            coordinator, entry, async_add_entities, current_barcodes
        )
    ]
    for parcel in coordinator.data or []:
        entities.append(
            TrunkrsParcelSensor(coordinator, entry, parcel.get("barcode", ""))
        )
    entities.append(TrunkrsNextDeliverySensor(coordinator, entry))
    entities.append(TrunkrsDeliveredParcelsSensor(coordinator, entry))
    entities.append(TrunkrsLastUpdateSensor(coordinator, entry))

    async_add_entities(entities)


class TrunkrsIncomingParcelsSensor(
    CoordinatorEntity[TrunkrsCoordinator], SensorEntity
):
    """Summary sensor: count of active (not-yet-delivered) tracked parcels.

    Spawns a per-parcel sensor for each new barcode and removes stale ones from
    the registry (via the registry, not self-removal, to avoid the ghost entity
    race).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by Trunkrs"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(
        self,
        coordinator: TrunkrsCoordinator,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
        known_barcodes: set[str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._async_add_entities = async_add_entities
        self._attr_unique_id = f"{entry.entry_id}_incoming_parcels"
        self._attr_device_info = build_device_info(entry)
        self._known_barcodes: set[str] = known_barcodes or set()

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self.coordinator.data or []}

    def _handle_coordinator_update(self) -> None:
        current_barcodes: set[str] = {
            p.get("barcode", "") for p in (self.coordinator.data or [])
        }

        new_barcodes = current_barcodes - self._known_barcodes
        if new_barcodes:
            self._async_add_entities(
                TrunkrsParcelSensor(self.coordinator, self._entry, barcode)
                for barcode in new_barcodes
            )

        removed_barcodes = self._known_barcodes - current_barcodes
        if removed_barcodes:
            registry = er.async_get(self.hass)
            for barcode in removed_barcodes:
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{self._entry.entry_id}_{barcode}"
                )
                if entity_id:
                    registry.async_remove(entity_id)

        self._known_barcodes = current_barcodes
        super()._handle_coordinator_update()


class TrunkrsParcelSensor(CoordinatorEntity[TrunkrsCoordinator], SensorEntity):
    """Per-parcel sensor reporting the status of a single tracked parcel."""

    _attr_has_entity_name = True
    _attr_translation_key = "parcel"
    _attr_attribution = "Data provided by Trunkrs"
    _unrecorded_attributes = frozenset({"raw", "history"})

    def __init__(
        self, coordinator: TrunkrsCoordinator, entry: ConfigEntry, barcode: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._barcode = barcode
        self._attr_unique_id = f"{entry.entry_id}_{barcode}"
        self._attr_translation_placeholders = {"barcode": barcode}
        self._attr_device_info = build_device_info(entry)

    def _get_parcel(self) -> dict[str, Any] | None:
        for parcel in self.coordinator.data or []:
            if parcel.get("barcode") == self._barcode:
                return parcel
        return None

    @property
    def native_value(self) -> str | None:
        parcel = self._get_parcel()
        return parcel.get("status") if parcel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        parcel = self._get_parcel()
        return dict(parcel) if parcel else {}


class TrunkrsNextDeliverySensor(CoordinatorEntity[TrunkrsCoordinator], SensorEntity):
    """Earliest expected delivery datetime across all active parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_delivery"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_attribution = "Data provided by Trunkrs"

    def __init__(self, coordinator: TrunkrsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_delivery"
        self._attr_device_info = build_device_info(entry)

    def _delivery_moments(self) -> list[tuple[datetime, dict]]:
        result: list[tuple[datetime, dict]] = []
        for parcel in self.coordinator.data or []:
            moment_str = parcel.get("planned_from")
            if not moment_str:
                continue
            try:
                dt = datetime.fromisoformat(moment_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result.append((dt, parcel))
            except ValueError:
                _LOGGER.debug("Could not parse delivery moment: %s", moment_str)
        return result

    @property
    def native_value(self) -> datetime | None:
        moments = self._delivery_moments()
        return min(dt for dt, _ in moments) if moments else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        moments = self._delivery_moments()
        if not moments:
            return {}
        _, earliest = min(moments, key=lambda x: x[0])
        return {
            "barcode": earliest.get("barcode"),
            "sender": earliest.get("sender"),
            "receiver": earliest.get("receiver"),
        }


class TrunkrsDeliveredParcelsSensor(
    CoordinatorEntity[TrunkrsCoordinator], SensorEntity
):
    """Recently delivered tracked Trunkrs parcels."""

    _attr_has_entity_name = True
    _attr_translation_key = "delivered_parcels"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = "Data provided by Trunkrs"
    _unrecorded_attributes = frozenset({"parcels"})

    def __init__(self, coordinator: TrunkrsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_delivered_parcels"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> int:
        return len(self.coordinator.delivered)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"parcels": self.coordinator.delivered}


class TrunkrsLastUpdateSensor(CoordinatorEntity[TrunkrsCoordinator], SensorEntity):
    """Diagnostic sensor reporting when Trunkrs was last polled successfully."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_attribution = "Data provided by Trunkrs"

    def __init__(self, coordinator: TrunkrsCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_update"
        self._attr_device_info = build_device_info(entry)

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_success_time
