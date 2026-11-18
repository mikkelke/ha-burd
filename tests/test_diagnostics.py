"""Tests for Burd diagnostics."""

from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.burd.diagnostics import async_get_config_entry_diagnostics
from custom_components.burd.parcels import normalize_parcel

from .payloads import ACTIVE_CODE, DELIVERED_CODE, TOKEN, active_sample


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    raw = active_sample()
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": ACTIVE_CODE, "token": TOKEN}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [normalize_parcel(raw, include_history=True)]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = {DELIVERED_CODE}
    entry.runtime_data.coordinator.final_codes = {"100000000000000003"}

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 2,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    text = repr(result)
    for secret in (ACTIVE_CODE, TOKEN, "ORD-0001", "Example Shop", "exampleshop"):
        assert secret not in text
    # non-identifying fields survive, or the diagnostics would be useless
    parcel = result["incoming"][0]
    assert parcel["status"] == "out_for_delivery"
    assert parcel["raw"]["statuses"][0]["statusType"] == "registered"
    assert parcel["raw"]["timeWindow"]["start"] == "2026-11-18T17:00:00Z"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()
    entry.runtime_data.coordinator.final_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
