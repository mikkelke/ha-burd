"""Tests for the Burd services (track_parcel / untrack_parcel)."""

from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.burd.api import BurdApiError, BurdTokenInfo
from custom_components.burd.const import (
    CONF_PARCELS,
    CONF_TOKEN,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from custom_components.burd.services import async_setup_services

from .payloads import ACTIVE_CODE, DELIVERED_CODE, TOKEN, active_sample

GET_PARCEL = "custom_components.burd.api.BurdApiClient.async_get_parcel"
RESOLVE = "custom_components.burd.api.BurdApiClient.async_resolve_token"
LINK = f"https://k.burd.dk/f/{TOKEN}"
INFO = BurdTokenInfo(barcode=ACTIVE_CODE, time_window=None, company_name=None)


@pytest.fixture(autouse=True)
def _no_network():
    with patch(GET_PARCEL, new=AsyncMock(return_value=active_sample())):
        yield


async def _setup(hass, parcels: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, value: str) -> None:
    await hass.services.async_call(
        DOMAIN, service, {CONF_TRACKING_CODE: value}, blocking=True
    )


async def test_track_number_adds_to_options(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", "1000 0000 0000 0000 01")
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_track_link_stores_number_and_token(hass):
    entry = await _setup(hass)
    with patch(RESOLVE, AsyncMock(return_value=INFO)):
        await _call(hass, "track_parcel", LINK)
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}
    ]


async def test_track_link_adds_the_token_to_a_tracked_number(hass):
    entry = await _setup(hass, [{CONF_TRACKING_CODE: ACTIVE_CODE}])
    with patch(RESOLVE, AsyncMock(return_value=INFO)):
        await _call(hass, "track_parcel", LINK)
    assert entry.options[CONF_PARCELS] == [
        {CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}
    ]


async def test_track_number_never_drops_a_saved_token(hass):
    saved = [{CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}]
    entry = await _setup(hass, saved)
    await _call(hass, "track_parcel", ACTIVE_CODE)
    assert entry.options[CONF_PARCELS] == saved


async def test_track_a_saved_link_needs_no_lookup(hass):
    saved = [{CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}]
    entry = await _setup(hass, saved)
    with patch(RESOLVE, AsyncMock()) as resolve:
        await _call(hass, "track_parcel", LINK)
    resolve.assert_not_awaited()
    assert entry.options[CONF_PARCELS] == saved


async def test_track_duplicate_number_is_noop(hass):
    entry = await _setup(hass, [{CONF_TRACKING_CODE: ACTIVE_CODE}])
    await _call(hass, "track_parcel", ACTIVE_CODE)
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


@pytest.mark.parametrize("value", ["", "https://example.com/x"])
async def test_track_rejects_bad_input(hass, value):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "track_parcel", value)


async def test_track_unknown_link_is_refused(hass):
    entry = await _setup(hass)
    with (
        patch(RESOLVE, AsyncMock(return_value=None)),
        pytest.raises(ServiceValidationError),
    ):
        await _call(hass, "track_parcel", LINK)
    assert entry.options[CONF_PARCELS] == []


async def test_track_link_while_burd_is_unreachable(hass):
    await _setup(hass)
    with (
        patch(RESOLVE, AsyncMock(side_effect=BurdApiError("ClientError"))),
        pytest.raises(HomeAssistantError, match="Could not reach Burd"),
    ):
        await _call(hass, "track_parcel", LINK)


async def test_untrack_by_number(hass):
    entry = await _setup(
        hass,
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}],
    )
    await _call(hass, "untrack_parcel", ACTIVE_CODE)
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: DELIVERED_CODE}]


async def test_untrack_by_saved_link_needs_no_lookup(hass):
    entry = await _setup(hass, [{CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}])
    with patch(RESOLVE, AsyncMock()) as resolve:
        await _call(hass, "untrack_parcel", LINK)
    resolve.assert_not_awaited()
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_unknown_code_is_noop(hass):
    entry = await _setup(hass, [{CONF_TRACKING_CODE: ACTIVE_CODE}])
    await _call(hass, "untrack_parcel", DELIVERED_CODE)
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_service_without_a_hub(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError, match="not set up"):
        await _call(hass, "track_parcel", ACTIVE_CODE)
