"""Tests for the Trunkrs entity platforms and diagnostics."""
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.trunkrs.const import (
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
)
from custom_components.trunkrs.device_trigger import (
    TRIGGER_EVENTS,
    TRIGGER_TYPES,
    async_get_triggers,
)
from custom_components.trunkrs.diagnostics import (
    async_get_config_entry_diagnostics,
)

_PAYLOAD = {"unknownField": "value", "name": "Jane Doe", "postalCode": "1234AB"}
_GET = "custom_components.trunkrs.api.TrunkrsApiClient.async_get_parcel"


async def _setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234AB",
        options={
            CONF_POSTAL_CODE: "1234AB",
            CONF_PARCELS: [{CONF_TRUNKRS_NR: "TR123456", CONF_POSTAL_CODE: "1234AB"}],
        },
    )
    entry.add_to_hass(hass)
    with patch(_GET, new=AsyncMock(return_value=_PAYLOAD)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


# --- sensors ---------------------------------------------------------------


async def test_all_expected_entities_are_created(hass):
    await _setup(hass)

    assert hass.states.get("sensor.trunkrs_1234ab_incoming_parcels").state == "1"
    assert hass.states.get("sensor.trunkrs_1234ab_delivered_parcels").state == "0"
    assert hass.states.get("sensor.trunkrs_1234ab_parcel_tr123456") is not None
    assert hass.states.get("sensor.trunkrs_1234ab_next_delivery") is not None
    assert hass.states.get("sensor.trunkrs_1234ab_last_successful_update") is not None
    assert hass.states.get("button.trunkrs_1234ab_refresh") is not None
    assert hass.states.get("calendar.trunkrs_1234ab_deliveries") is not None


async def test_per_parcel_sensor_reports_unknown_until_mapped(hass):
    """The documented gap, visible at the entity level."""
    await _setup(hass)
    state = hass.states.get("sensor.trunkrs_1234ab_parcel_tr123456")
    assert state.state == "unknown"
    assert state.attributes["barcode"] == "TR123456"


async def test_next_delivery_is_unknown_without_an_eta(hass):
    await _setup(hass)
    # planned_from is None until the payload is mapped.
    assert hass.states.get("sensor.trunkrs_1234ab_next_delivery").state == "unknown"


async def test_summary_sensor_exposes_the_parcel_list(hass):
    await _setup(hass)
    parcels = hass.states.get("sensor.trunkrs_1234ab_incoming_parcels").attributes[
        "parcels"
    ]
    assert [p["barcode"] for p in parcels] == ["TR123456"]


# --- button ----------------------------------------------------------------


async def test_refresh_button_triggers_a_poll(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data.coordinator

    with patch.object(
        coordinator, "async_request_refresh", new=AsyncMock()
    ) as refresh:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.trunkrs_1234ab_refresh"},
            blocking=True,
        )
    refresh.assert_awaited_once()


# --- calendar --------------------------------------------------------------


async def test_calendar_is_empty_without_delivery_windows(hass):
    """No ETA in the mapping yet → no events. It works once that lands."""
    await _setup(hass)
    assert hass.states.get("calendar.trunkrs_1234ab_deliveries").state == "off"


# --- device triggers -------------------------------------------------------


async def test_device_triggers_cover_every_fired_event():
    """Each bus event the coordinator fires is exposed as a device trigger."""
    assert TRIGGER_TYPES == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    }
    for trigger_type, event in TRIGGER_EVENTS.items():
        assert event == f"{DOMAIN}_{trigger_type}"


async def test_async_get_triggers_returns_all_types(hass):
    triggers = await async_get_triggers(hass, "device-id")
    assert {t["type"] for t in triggers} == TRIGGER_TYPES
    assert all(t["domain"] == DOMAIN for t in triggers)


# --- diagnostics -----------------------------------------------------------


async def test_diagnostics_preserve_the_raw_payload(hass):
    """Diagnostics is how users hand us the payload needed to finish this."""
    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["counts"] == {"incoming_active": 1, "delivered": 0}
    assert "unknownField" in diag["incoming"][0]["raw"]
    # The sharing note must point users at the issue tracker.
    assert "issues/new" in diag["note"]


async def test_diagnostics_redact_personal_fields(hass):
    """Redaction is best-effort on common key names — verify it does fire."""
    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    raw = diag["incoming"][0]["raw"]
    assert raw["name"] == "**REDACTED**"
    assert raw["postalCode"] == "**REDACTED**"
    # our own stored credentials too
    assert diag["entry_options"]["postal_code"] == "**REDACTED**"
