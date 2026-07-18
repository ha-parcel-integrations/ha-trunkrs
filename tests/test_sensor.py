"""Tests for the Trunkrs sensor platform's dynamic per-parcel entities.

Like the calendar, these paths are payload-independent: they operate on
*normalised* parcel dicts, so they can be exercised fully today by driving the
coordinator directly.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.const import (
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
)
from custom_components.trunkrs.coordinator import TrunkrsCoordinator
from custom_components.trunkrs.sensor import (
    TrunkrsIncomingParcelsSensor,
    TrunkrsNextDeliverySensor,
    TrunkrsParcelSensor,
)

_GET = "custom_components.trunkrs.api.TrunkrsApiClient.async_get_parcel"


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="1234AB", options={CONF_POSTAL_CODE: "1234AB"}
    )
    entry.add_to_hass(hass)
    return entry


def _coordinator(hass, entry, parcels) -> TrunkrsCoordinator:
    coordinator = TrunkrsCoordinator(hass, AsyncMock(), entry)
    coordinator.data = parcels
    return coordinator


def _parcel(barcode: str, **extra) -> dict:
    return {"barcode": barcode, "status": "in_transit", **extra}


# --- per-parcel sensor -----------------------------------------------------


async def test_parcel_sensor_reads_its_own_parcel(hass):
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry, [_parcel("TR1"), _parcel("TR2")])
    sensor = TrunkrsParcelSensor(coordinator, entry, "TR2")

    assert sensor.native_value == "in_transit"
    assert sensor.extra_state_attributes["barcode"] == "TR2"


async def test_parcel_sensor_is_none_when_its_parcel_disappears(hass):
    """A parcel dropping out must not raise — the entity just goes empty."""
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry, [])
    sensor = TrunkrsParcelSensor(coordinator, entry, "GONE")

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


# --- summary sensor: spawning and removing per-parcel entities -------------


async def test_summary_spawns_sensors_for_new_barcodes(hass):
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry, [_parcel("TR1")])
    added: list = []
    summary = TrunkrsIncomingParcelsSensor(
        coordinator, entry, lambda entities: added.extend(entities), {"TR1"}
    )
    summary.hass = hass

    coordinator.data = [_parcel("TR1"), _parcel("TR2")]
    with patch.object(TrunkrsIncomingParcelsSensor, "async_write_ha_state"):
        summary._handle_coordinator_update()

    assert [s._barcode for s in added] == ["TR2"]


async def test_summary_removes_sensors_for_gone_barcodes(hass):
    """Removal goes through the registry, not entity self-removal.

    Self-removal races with coordinator-listener cleanup and leaves ghost
    entities behind — the suite-wide rule is to remove via the registry.
    """
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry, [_parcel("TR1")])
    summary = TrunkrsIncomingParcelsSensor(
        coordinator, entry, lambda entities: None, {"TR1"}
    )
    summary.hass = hass

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_TR1", config_entry=entry
    )
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_TR1"
    )
    assert entity_id is not None

    coordinator.data = []
    with patch.object(TrunkrsIncomingParcelsSensor, "async_write_ha_state"):
        summary._handle_coordinator_update()

    assert registry.async_get(entity_id) is None


# --- next delivery ---------------------------------------------------------


async def test_next_delivery_picks_the_earliest(hass):
    entry = _entry(hass)
    base = datetime.now(timezone.utc)
    coordinator = _coordinator(
        hass,
        entry,
        [
            _parcel("late", planned_from=(base + timedelta(hours=5)).isoformat()),
            _parcel(
                "soon",
                planned_from=(base + timedelta(hours=1)).isoformat(),
                sender="Shop",
                receiver="Me",
            ),
        ],
    )
    sensor = TrunkrsNextDeliverySensor(coordinator, entry)

    assert sensor.native_value == base + timedelta(hours=1)
    assert sensor.extra_state_attributes["barcode"] == "soon"
    assert sensor.extra_state_attributes["sender"] == "Shop"


async def test_next_delivery_ignores_unparseable_moments(hass):
    entry = _entry(hass)
    coordinator = _coordinator(hass, entry, [_parcel("bad", planned_from="nope")])
    sensor = TrunkrsNextDeliverySensor(coordinator, entry)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


async def test_next_delivery_assumes_utc_for_naive_timestamps(hass):
    entry = _entry(hass)
    coordinator = _coordinator(
        hass, entry, [_parcel("naive", planned_from="2026-05-01T10:00:00")]
    )
    sensor = TrunkrsNextDeliverySensor(coordinator, entry)

    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)


# --- setup-time cleanup of stale entities ----------------------------------


async def test_setup_removes_stale_parcel_sensors_but_keeps_the_others(hass):
    """Only per-parcel sensors are pruned — never the button or diagnostics."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234AB",
        options={
            CONF_POSTAL_CODE: "1234AB",
            CONF_PARCELS: [{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}],
        },
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_OLDPARCEL", config_entry=entry
    )
    button = registry.async_get_or_create(
        "button", DOMAIN, f"{entry.entry_id}_refresh", config_entry=entry
    )

    with patch(_GET, new=AsyncMock(return_value={"x": 1})):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The stale per-parcel sensor is gone...
    assert registry.async_get(stale.entity_id) is None
    # ...and the refresh button (same unique-id prefix, different domain) is not.
    assert registry.async_get(button.entity_id) is not None
    assert hass.states.get("sensor.trunkrs_1234ab_last_successful_update") is not None
