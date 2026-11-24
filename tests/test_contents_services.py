"""Tests for the contents action, its routing across hubs and the store lifecycle."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import voluptuous as vol

from custom_components.trunkrs.const import (
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    CONF_TRUNKRS_NR,
    DOMAIN,
)
from custom_components.trunkrs.coordinator import TrunkrsCoordinator
from custom_components.trunkrs.services import (
    SERVICE_TRACK_PARCEL,
    SERVICE_UNTRACK_PARCEL,
    SERVICE_UPDATE_CONTENTS,
    async_setup_services,
)

from .payloads import IN_TRANSIT

_GET = "custom_components.trunkrs.api.TrunkrsApiClient.async_get_parcel"
_VERIFY = "custom_components.trunkrs.services.async_verify_parcel"
_REFRESH = f"{TrunkrsCoordinator.__module__}.TrunkrsCoordinator.async_request_refresh"
CODE = "TR123456"
OTHER = "TR777777"
UNTRACKED = "TR555555"
STORE_KEY = f"{DOMAIN}.contents.%s"


def _hub(postcode: str = "1234AB", codes: tuple[str, ...] = (CODE,)) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=postcode,
        options={
            CONF_POSTAL_CODE: postcode,
            CONF_PARCELS: [{CONF_TRUNKRS_NR: code} for code in codes],
        },
    )


async def _setup(hass, *entries: MockConfigEntry) -> list[MockConfigEntry]:
    entries = entries or (_hub(),)
    for entry in entries:
        entry.add_to_hass(hass)
    with patch(_GET, new=AsyncMock(return_value=IN_TRANSIT)):
        assert await hass.config_entries.async_setup(entries[0].entry_id)
        await hass.async_block_till_done()
    return list(entries)


async def _call(hass, service: str, data: dict) -> None:
    with patch(_GET, new=AsyncMock(return_value=IN_TRANSIT)):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


async def _update(hass, data: dict) -> None:
    await _call(hass, SERVICE_UPDATE_CONTENTS, data)


def _contents(entry: MockConfigEntry, code: str = CODE) -> str | None:
    parcels = {p["barcode"]: p for p in entry.runtime_data.coordinator.data}
    return parcels[code]["contents"]


def _stored(entry: MockConfigEntry, code: str) -> str | None:
    return entry.runtime_data.coordinator.contents_store.get(code)


# --- single entry ----------------------------------------------------------


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    (entry,) = await _setup(hass)
    assert _contents(entry) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    (entry,) = await _setup(hass)

    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": " Running  shoes "})
    assert _contents(entry) == "Running shoes"

    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "Winter socks"})
    assert _contents(entry) == "Winter socks"

    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "  "})
    assert _contents(entry) is None


async def test_update_contents_without_text_clears(hass):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"})

    await _update(hass, {CONF_TRACKING_CODE: CODE})

    assert _contents(entry) is None


async def test_update_contents_normalizes_the_code(hass):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: f" {CODE.lower()} ", "contents": "Shoes"})
    assert _contents(entry) == "Shoes"


async def test_update_contents_trims_and_caps_the_text(hass):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "x" * 500})
    assert _contents(entry) == "x" * 120


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"})
    assert _stored(entry, UNTRACKED) == "Running shoes"


async def test_update_contents_refreshes_only_when_something_changed(hass):
    await _setup(hass)
    data = {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    with patch(_REFRESH, new=AsyncMock()) as refresh:
        await _call(hass, SERVICE_UPDATE_CONTENTS, data)
        assert refresh.await_count == 1
        await _call(hass, SERVICE_UPDATE_CONTENTS, data)
        assert refresh.await_count == 1
        await _call(hass, SERVICE_UPDATE_CONTENTS, {CONF_TRACKING_CODE: OTHER})
        assert refresh.await_count == 1


async def test_unchanged_value_writes_nothing(hass, hass_storage):
    (entry,) = await _setup(hass)
    data = {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"}
    await _update(hass, data)
    hass_storage.pop(STORE_KEY % entry.entry_id, None)

    await _update(hass, data)

    assert STORE_KEY % entry.entry_id not in hass_storage


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _update(hass, {CONF_TRACKING_CODE: "  ", "contents": "Shoes"})


async def test_update_contents_without_a_loaded_entry_raises(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_UPDATE_CONTENTS,
            {CONF_TRACKING_CODE: CODE, "contents": "Shoes"},
            blocking=True,
        )


# --- several hubs ----------------------------------------------------------


async def test_update_contents_goes_to_every_hub_that_lists_the_code(hass):
    first, second, third = await _setup(
        hass, _hub("1234AB"), _hub("5678CD"), _hub("9012EF", codes=(OTHER,))
    )

    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"})

    assert _stored(first, CODE) == "Running shoes"
    assert _stored(second, CODE) == "Running shoes"
    assert _stored(third, CODE) is None


async def test_update_contents_with_several_hubs_and_no_match_raises(hass):
    first, second = await _setup(hass, _hub("1234AB"), _hub("5678CD"))

    with pytest.raises(ServiceValidationError) as err:
        await _update(hass, {CONF_TRACKING_CODE: UNTRACKED, "contents": "Shoes"})

    assert err.value.translation_key == "update_contents_entry_required"
    assert _stored(first, UNTRACKED) is None
    assert _stored(second, UNTRACKED) is None


async def test_config_entry_id_targets_one_hub(hass):
    first, second = await _setup(hass, _hub("1234AB"), _hub("5678CD"))

    await _update(
        hass,
        {
            CONF_TRACKING_CODE: CODE,
            "contents": "Running shoes",
            "config_entry_id": second.entry_id,
        },
    )

    assert _stored(first, CODE) is None
    assert _stored(second, CODE) == "Running shoes"


async def test_config_entry_id_covers_a_code_no_hub_lists(hass):
    first, second = await _setup(hass, _hub("1234AB"), _hub("5678CD"))

    await _update(
        hass,
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Shoes",
            "config_entry_id": first.entry_id,
        },
    )

    assert _stored(first, UNTRACKED) == "Shoes"
    assert _stored(second, UNTRACKED) is None


async def test_unknown_config_entry_id_raises(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _update(
            hass,
            {CONF_TRACKING_CODE: CODE, "contents": "Shoes", "config_entry_id": "nope"},
        )


async def test_postal_code_is_not_a_routing_field_of_update_contents(hass):
    await _setup(hass)
    with pytest.raises(vol.Invalid):
        await _update(
            hass,
            {CONF_TRACKING_CODE: CODE, "contents": "Shoes", "postal_code": "1234AB"},
        )


async def test_a_single_loaded_hub_is_targeted_directly(hass):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: UNTRACKED, "contents": "Shoes"})
    assert _stored(entry, UNTRACKED) == "Shoes"


async def test_an_unloaded_hub_is_not_a_target(hass):
    first, second = await _setup(hass, _hub("1234AB"), _hub("5678CD"))
    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert second.state is ConfigEntryState.NOT_LOADED

    await _update(hass, {CONF_TRACKING_CODE: UNTRACKED, "contents": "Shoes"})

    assert _stored(first, UNTRACKED) == "Shoes"


async def test_known_codes_hold_tracked_codes_and_parcels_the_entry_has(hass):
    (entry,) = await _setup(hass, _hub(codes=(CODE, OTHER)))
    coordinator = entry.runtime_data.coordinator
    assert coordinator.known_codes == {CODE, OTHER}

    coordinator.delivered = [{"barcode": " tr999999 "}]
    assert coordinator.known_codes == {CODE, OTHER, "TR999999"}


# --- track_parcel / untrack_parcel -----------------------------------------


async def test_track_parcel_stores_contents_in_the_chosen_hub(hass):
    first, second = await _setup(
        hass, _hub("1234AB", codes=()), _hub("5678CD", codes=())
    )

    with patch(_VERIFY, new=AsyncMock(return_value=True)):
        await _call(
            hass,
            SERVICE_TRACK_PARCEL,
            {
                CONF_TRACKING_CODE: "tr123456",
                CONF_POSTAL_CODE: "5678cd",
                "contents": " Running shoes ",
            },
        )

    assert _stored(first, CODE) is None
    assert _stored(second, CODE) == "Running shoes"
    assert _contents(second) == "Running shoes"


async def test_track_parcel_stores_nothing_when_verification_fails(hass):
    (entry,) = await _setup(hass, _hub(codes=()))

    with (
        patch(_VERIFY, new=AsyncMock(return_value=False)),
        pytest.raises(ServiceValidationError),
    ):
        await _call(
            hass,
            SERVICE_TRACK_PARCEL,
            {CONF_TRACKING_CODE: CODE, "contents": "Shoes"},
        )

    assert _stored(entry, CODE) is None


async def test_track_parcel_without_contents_leaves_them_alone(hass):
    (entry,) = await _setup(hass, _hub(codes=()))

    with patch(_VERIFY, new=AsyncMock(return_value=True)):
        await _call(hass, SERVICE_TRACK_PARCEL, {CONF_TRACKING_CODE: CODE})

    assert _contents(entry) is None


async def test_track_parcel_on_a_tracked_code_sets_contents_and_refreshes(hass):
    (entry,) = await _setup(hass)

    with patch(_REFRESH, new=AsyncMock()) as refresh:
        await _call(
            hass,
            SERVICE_TRACK_PARCEL,
            {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
        )
        assert refresh.await_count == 1
        await _call(
            hass,
            SERVICE_TRACK_PARCEL,
            {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"},
        )
        assert refresh.await_count == 1

    assert _stored(entry, CODE) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRUNKRS_NR: CODE}]


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    async_setup_services(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_TRACK_PARCEL,
            {CONF_TRACKING_CODE: CODE, "contents": "Shoes"},
            blocking=True,
        )


async def test_untrack_drops_the_value_from_every_hub_that_held_it(hass):
    first, second = await _setup(hass, _hub("1234AB"), _hub("5678CD"))
    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"})

    await _call(hass, SERVICE_UNTRACK_PARCEL, {CONF_TRACKING_CODE: CODE})

    assert _stored(first, CODE) is None
    assert _stored(second, CODE) is None


async def test_untrack_of_an_unloaded_hub_does_not_touch_a_store(hass):
    entry = _hub()
    entry.add_to_hass(hass)
    async_setup_services(hass)

    await hass.services.async_call(
        DOMAIN, SERVICE_UNTRACK_PARCEL, {CONF_TRACKING_CODE: CODE}, blocking=True
    )

    assert entry.options[CONF_PARCELS] == []


# --- lifecycle -------------------------------------------------------------


async def test_services_include_update_contents_and_go_with_the_last_hub(hass):
    first, second = await _setup(hass, _hub("1234AB"), _hub("5678CD"))
    assert hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS)

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS)

    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS)


async def test_stored_contents_survive_a_reload(hass, hass_storage):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert hass_storage[STORE_KEY % entry.entry_id]["data"] == {
        "by_code": {CODE: "Running shoes"}
    }

    with patch(_GET, new=AsyncMock(return_value=IN_TRANSIT)):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert _contents(entry) == "Running shoes"


async def test_removing_the_entry_deletes_its_contents(hass, hass_storage):
    (entry,) = await _setup(hass)
    await _update(hass, {CONF_TRACKING_CODE: CODE, "contents": "Running shoes"})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert STORE_KEY % entry.entry_id in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert STORE_KEY % entry.entry_id not in hass_storage


async def test_the_store_drops_the_oldest_code_past_the_cap(hass):
    (entry,) = await _setup(hass)
    for number in range(201):
        await _update(
            hass,
            {CONF_TRACKING_CODE: f"CODE{number}", "contents": f"Item {number}"},
        )

    assert _stored(entry, "CODE0") is None
    assert _stored(entry, "CODE1") == "Item 1"
    assert _stored(entry, "CODE200") == "Item 200"
