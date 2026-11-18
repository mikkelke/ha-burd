"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.burd.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.burd.parcels import (
    apply_delivered_filter,
    build_history,
    is_final,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sender_name,
    sort_parcels_by_ts,
    to_utc_iso,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    pickup_sample,
    status,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("registered", ParcelStatus.REGISTERED),
        ("notReceived", ParcelStatus.REGISTERED),
        ("received", ParcelStatus.IN_TRANSIT),
        ("receivedFlex", ParcelStatus.IN_TRANSIT),
        ("deliveryOnTheWay", ParcelStatus.OUT_FOR_DELIVERY),
        ("delivered", ParcelStatus.DELIVERED),
        ("deliveryFailed", ParcelStatus.PROBLEM),
        ("returnToSender", ParcelStatus.RETURNING),
        ("canceled", ParcelStatus.PROBLEM),
        # the labels endpoint spells them PascalCase
        ("DeliveryOnTheWay", ParcelStatus.OUT_FOR_DELIVERY),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("teleported") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("somethingNew") is None
    assert map_event_status("delivered") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("abducted") == ParcelStatus.UNKNOWN
    assert map_parcel_status("abducted") == ParcelStatus.UNKNOWN
    assert caplog.text.count("abducted") == 1
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == UTC
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_utc_iso_keeps_offsets():
    assert to_utc_iso("2026-11-17T19:41:08Z", "created") == "2026-11-17T19:41:08+00:00"
    assert (
        to_utc_iso("2026-11-17T20:41:08+01:00", "created")
        == "2026-11-17T20:41:08+01:00"
    )
    assert to_utc_iso(None, "created") is None
    assert to_utc_iso(17, "created") is None


def test_to_utc_iso_reads_naive_as_utc_and_warns_once(caplog):
    assert to_utc_iso("2026-11-17T19:41:08.123", "created") == (
        "2026-11-17T19:41:08.123000+00:00"
    )
    to_utc_iso("2026-11-17T19:41:09", "created")
    assert caplog.text.count("created has no UTC offset") == 1
    # field name only, never the value
    assert "19:41" not in caplog.text


def test_to_utc_iso_unparseable_warns_with_field_name(caplog):
    assert to_utc_iso("tomorrow", "timeWindow.start") is None
    assert "unparseable timestamp in timeWindow.start" in caplog.text
    assert "tomorrow" not in caplog.text


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest_with_burds_text():
    raw = delivered_sample()
    history = build_history(list(reversed(raw["statuses"])), raw["descriptions"])
    assert [entry["status"] for entry in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert history[0]["raw_status"] == "Waiting for the webshop."
    assert history[-1]["timestamp"] == "2026-11-17T19:41:08+00:00"


def test_build_history_caps_to_max_events():
    statuses = [
        status("received", f"2026-11-{day:02d}T10:00:00Z") for day in range(1, 26)
    ]
    assert len(build_history(statuses, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"statusType": "received"}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_falls_back_to_display_text_then_status_type():
    history = build_history(
        [
            {**status("received", "2026-11-16T10:00:00Z"), "displayText": "Modtaget"},
            status("receivedFlex", "2026-11-17T10:00:00Z"),
        ]
    )
    assert [entry["raw_status"] for entry in history] == ["Modtaget", "receivedFlex"]


# ---------------------------------------------------------------------------
# sender_name / is_final
# ---------------------------------------------------------------------------


def test_sender_name_title_cases_unknown_ids():
    assert sender_name("proshop") == "Proshop"
    assert sender_name("postnord") == "PostNord"
    assert sender_name("coolshop.dk") == "Coolshop.dk"
    assert sender_name("") is None
    assert sender_name(None) is None


def test_is_final_reads_the_newest_status():
    raw = active_sample()
    assert is_final(raw) == (False, "2026-11-17T16:02:44+00:00")
    raw["statuses"].append(status("canceled", "2026-11-18T08:00:00Z"))
    assert is_final(raw) == (True, "2026-11-18T08:00:00+00:00")
    assert is_final({}) == (False, None)


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    """The docs site would have to pick one; "awaiting data" must not hide a confirmed field."""
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample.

    Copy this test into a real carrier's own test_parcels.py verbatim — it
    stays correct for whatever subset of CAPABILITIES that carrier declares.
    """
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    assert pickup["pickup_point"] is None  # door delivery only
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Burd"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["sender"] == "Exampleshop"
    assert parcel["receiver"] is None
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered."
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-11-17T19:41:08+00:00"
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["url"] == f"https://burd.dk/track?code={DELIVERED_CODE}"
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 4
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_sorts_statuses_whatever_order_burd_sends():
    raw = delivered_sample()
    raw["statuses"].reverse()
    assert normalize_parcel(raw)["status"] == ParcelStatus.DELIVERED


def test_normalize_active_parcel_has_the_min_levering_window():
    parcel = normalize_parcel(active_sample())
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["sender"] == "Example Shop"
    assert parcel["planned_from"] == "2026-11-18T17:00:00+00:00"
    assert parcel["planned_to"] == "2026-11-18T21:00:00+00:00"


def test_normalize_collapses_point_estimate_to_no_window_end():
    raw = active_sample()
    raw["timeWindow"]["end"] = raw["timeWindow"]["start"]
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == "2026-11-18T17:00:00+00:00"
    assert parcel["planned_to"] is None


@pytest.mark.parametrize("final", ["canceled", "returnToSender"])
def test_normalize_clears_the_window_once_final(final):
    raw = active_sample()
    raw["statuses"].append(status(final, "2026-11-18T08:00:00Z"))
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_unmapped_newest_falls_back_to_history(caplog):
    raw = active_sample()
    raw["statuses"].append(status("teleported", "2026-11-18T08:00:00Z"))
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "teleported"
    assert "status=teleported" in caplog.text


def test_normalize_unmapped_only_status_is_unknown():
    raw = {"number": ACTIVE_CODE, "statuses": [status("teleported", "2026-11-18Z")]}
    assert normalize_parcel(raw)["status"] == ParcelStatus.UNKNOWN


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-known number still yields a full parcel dict."""
    parcel = normalize_parcel({}, tracking_code=ACTIVE_CODE)
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["sender"] is None
    assert parcel["url"] == f"https://burd.dk/track?code={ACTIVE_CODE}"
    assert parcel["history"] is None


def test_normalize_keeps_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
