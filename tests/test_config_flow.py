"""Tests for the Burd config and options flow."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.burd.api import BurdApiError, BurdTokenInfo
from custom_components.burd.config_flow import (
    InvalidTrackingInput,
    TrackingInput,
    display_entry,
    normalize_token,
    normalize_tracking_code,
    parse_tracking_input,
    valid_tracking_code,
)
from custom_components.burd.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TOKEN,
    CONF_TRACKING_CODE,
    DOMAIN,
)

from .payloads import ACTIVE_CODE, DELIVERED_CODE, TOKEN

RESOLVE = "custom_components.burd.api.BurdApiClient.async_resolve_token"
INFO = BurdTokenInfo(barcode=ACTIVE_CODE, time_window=None, company_name=None)


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code("1000 0000-0000 0000 01") == "100000000000000001"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_normalize_token_never_recases():
    assert normalize_token("  AbC  ") == "AbC"
    assert normalize_token(" ") is None


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code(ACTIVE_CODE)
    assert valid_tracking_code("ABC")
    assert not valid_tracking_code("")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (ACTIVE_CODE, TrackingInput(ACTIVE_CODE, None)),
        ("1000 0000 0000 0000 01", TrackingInput(ACTIVE_CODE, None)),
        (f"https://k.burd.dk/f/{TOKEN}", TrackingInput(None, TOKEN)),
        (f"k.burd.dk/f/{TOKEN}?a=poa", TrackingInput(None, TOKEN)),
        (f"https://k.burd.dk/{TOKEN}", TrackingInput(None, TOKEN)),
        (f"https://k.burd.dk/{TOKEN}/confirmed", TrackingInput(None, TOKEN)),
        (f"https://k.burd.dk/e/{TOKEN}/confirm", TrackingInput(None, TOKEN)),
        (
            f"https://www.burd.dk/track?code={ACTIVE_CODE}",
            TrackingInput(ACTIVE_CODE, None),
        ),
        (f"{ACTIVE_CODE}/{TOKEN}", TrackingInput(ACTIVE_CODE, TOKEN)),
        (TOKEN, TrackingInput(None, TOKEN, token_only=True)),
    ],
)
def test_parse_tracking_input(value, expected):
    assert parse_tracking_input(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "invalid_tracking_code"),
        ("two words", "invalid_tracking_code"),
        (f"{ACTIVE_CODE}/", "invalid_tracking_code"),
        ("https://k.burd.dk/", "unsupported_link"),
        ("https://burd.dk/track", "unsupported_link"),
        ("https://example.com/f/abc", "unsupported_link"),
    ],
)
def test_parse_tracking_input_refuses(value, reason):
    with pytest.raises(InvalidTrackingInput) as err:
        parse_tracking_input(value)
    assert err.value.reason == reason


def test_display_entry_shows_the_token_after_the_number():
    assert display_entry({CONF_TRACKING_CODE: ACTIVE_CODE}) == ACTIVE_CODE
    assert (
        display_entry({CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN})
        == f"{ACTIVE_CODE}/{TOKEN}"
    )


async def test_user_flow_creates_hub_without_input(hass):
    """No account, no postcode — the entry is created straight away."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Burd"
    assert result["options"][CONF_PARCELS] == []


async def test_second_hub_rejected(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    # single_config_entry in the manifest aborts before the flow runs.
    assert result["reason"] == "single_instance_allowed"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels},
    )


def _settings_input(
    *,
    history=False,
    filter_type="days",
    amount=7,
) -> dict:
    """Build the settings-form submission."""
    return {
        CONF_DELIVERED_FILTER_TYPE: filter_type,
        CONF_DELIVERED_FILTER_AMOUNT: amount,
        CONF_INCLUDE_HISTORY: history,
    }


async def _open_options_step(hass, entry, step_id: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def _submit(hass, entry, lines):
    result = await _open_options_step(hass, entry, "parcels")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": lines}
    )


async def test_options_add_number_needs_no_lookup(hass):
    """Burd answers "unknown" until the webshop registers it, so no lookup."""
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(RESOLVE, AsyncMock()) as resolve:
        result = await _submit(hass, entry, ["1000 0000 0000 0000 01"])
    resolve.assert_not_awaited()
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: ACTIVE_CODE}]


async def test_options_add_link_stores_number_and_token(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(RESOLVE, AsyncMock(return_value=INFO)):
        result = await _submit(hass, entry, [f"https://k.burd.dk/f/{TOKEN}"])
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}
    ]


async def test_options_unknown_link_is_refused(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(RESOLVE, AsyncMock(return_value=None)):
        result = await _submit(hass, entry, [f"https://k.burd.dk/f/{TOKEN}"])
    assert result["type"] == "form"
    assert result["errors"] == {"base": "not_recognised"}


async def test_options_unreachable_burd_is_reported(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(RESOLVE, AsyncMock(side_effect=BurdApiError("ClientError"))):
        result = await _submit(hass, entry, [f"https://k.burd.dk/f/{TOKEN}"])
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_number_and_token_must_agree(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(RESOLVE, AsyncMock(return_value=INFO)):
        result = await _submit(hass, entry, [f"{DELIVERED_CODE}/{TOKEN}"])
    assert result["errors"] == {"base": "token_mismatch"}


async def test_options_bare_value_unknown_as_token_is_kept_as_a_number(hass):
    """Not every Burd number need be digits; a bad one just stays unknown."""
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(RESOLVE, AsyncMock(return_value=None)):
        result = await _submit(hass, entry, ["abc123"])
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "ABC123"}]


async def test_options_bad_line_is_refused(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _submit(hass, entry, ["https://example.com/x"])
    assert result["errors"] == {"base": "unsupported_link"}


async def test_options_saved_lines_are_not_looked_up_again(hass):
    """A saved link keeps working in the form after it has expired at Burd."""
    saved = [{CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}]
    entry = _hub(saved)
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    suggested = result["data_schema"].schema
    assert any(
        key.description["suggested_value"] == [f"{ACTIVE_CODE}/{TOKEN}"]
        for key in suggested
    )
    with patch(RESOLVE, AsyncMock()) as resolve:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"tracking_codes": [f"{ACTIVE_CODE}/{TOKEN}", f"k.burd.dk/f/{TOKEN}"]},
        )
    resolve.assert_not_awaited()
    assert result["data"][CONF_PARCELS] == saved


async def test_options_de_duplicates_and_keeps_the_token(hass):
    entry = _hub([{CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}])
    entry.add_to_hass(hass)
    result = await _submit(hass, entry, [f"{ACTIVE_CODE}/{TOKEN}", ACTIVE_CODE])
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}
    ]


async def test_options_remove_parcel(hass):
    entry = _hub(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    result = await _submit(hass, entry, [DELIVERED_CODE])
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: DELIVERED_CODE}]


async def test_options_can_clear_the_tracked_code_list(hass):
    entry = _hub([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    result = await _submit(hass, entry, [])
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == []


async def test_options_changes_history_and_delivered(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _settings_input(
            history=True,
            filter_type="parcels",
            amount=5,
        ),
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5


async def test_settings_keep_the_saved_tokens(hass):
    saved = [{CONF_TRACKING_CODE: ACTIVE_CODE, CONF_TOKEN: TOKEN}]
    entry = _hub(saved)
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings_input()
    )
    assert result["data"][CONF_PARCELS] == saved
