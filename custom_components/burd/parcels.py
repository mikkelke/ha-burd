"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The carrier-specific parts are :data:`_STATUS_MAP`, the history builder and
:func:`normalize_parcel`. Everything else (the sort contract, the delivered
filter, the one-shot warnings) is suite-wide machinery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

CARRIER_NAME = "Burd"

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/mikkelke/ha-burd/issues/new?template=unrecognised_status.yml"
)

# Burd's status types, lower-cased: the status list spells them camelCase,
# the labels endpoint PascalCase, so every lookup is case-insensitive.
_STATUS_MAP: dict[str, ParcelStatus] = {
    # The webshop announced it; Burd does not have it yet.
    "registered": ParcelStatus.REGISTERED,
    # Burd expected it today but did not get it from the webshop; it is
    # still on its way to Burd, not a fault with the delivery.
    "notreceived": ParcelStatus.REGISTERED,
    # At Burd's terminal.
    "received": ParcelStatus.IN_TRANSIT,
    # At Burd, delivery moved to another evening.
    "receivedflex": ParcelStatus.IN_TRANSIT,
    "deliveryontheway": ParcelStatus.OUT_FOR_DELIVERY,
    "delivered": ParcelStatus.DELIVERED,
    # Burd retries within two working days before returning it.
    "deliveryfailed": ParcelStatus.PROBLEM,
    "returntosender": ParcelStatus.RETURNING,
    "canceled": ParcelStatus.PROBLEM,
}

# Status types after which Burd does nothing more with the shipment, other
# than delivered (which the coordinator already skips on its own).
FINAL_STATUS_TYPES = frozenset({"returntosender", "canceled"})

# A few webshop ids whose title-cased form would be wrong.
_SENDER_NAMES = {"postnord": "PostNord"}

# Keys already warned about, so each one-shot warning fires once per session.
_warned: set[str] = set()


def warn_once(key: str, message: str, *args: Any) -> None:
    """Log a one-shot WARNING with a copy-paste issue link.

    ``message`` names keys, codes or field names only, never a value that
    could identify a person, an address or a shipment.
    """
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(
        "Unrecognised Burd data — help us handle it. Open an issue "
        "and paste this line: %s\n  %s",
        NEW_ISSUE_URL,
        message % args if args else message,
    )


def _status_key(code: str | None) -> str | None:
    return code.lower() if isinstance(code, str) and code else None


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    warn_once("status:" + code, "status=%s → reported as 'unknown'", code)


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a Burd status type to a canonical :class:`ParcelStatus`.

    ``None`` reports ``unknown`` silently; an unrecognised type reports
    ``unknown`` with a one-shot warning.
    """
    key = _status_key(code)
    if key is None:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(key)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(str(code))
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status type to a canonical status, or ``None``.

    Unmapped types keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    key = _status_key(code)
    if key is None:
        return None
    mapped = _STATUS_MAP.get(key)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(str(code))
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def to_utc_iso(value: Any, field: str) -> str | None:
    """Return Burd's timestamp as an aware ISO 8601 string, or ``None``.

    Burd stamps in UTC. A value without an offset is read as UTC and named
    once in a warning (field name only), so a real one can confirm it.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        warn_once("unparseable:" + field, "unparseable timestamp in %s", field)
        return None
    if parsed.tzinfo is None:
        warn_once(
            "naive:" + field, "%s has no UTC offset; read as UTC, please confirm", field
        )
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _ordered_statuses(statuses: list | None) -> list[tuple[str | None, dict]]:
    """Return ``(created_iso, entry)`` pairs, oldest first.

    Entries without a parseable ``created`` keep their place after the dated
    ones, in the order Burd sent them.
    """
    dated: list[tuple[datetime, str, dict]] = []
    undated: list[tuple[str | None, dict]] = []
    for entry in statuses or []:
        if not isinstance(entry, dict):
            continue
        created = to_utc_iso(entry.get("created"), "created")
        parsed = parse_iso(created)
        if created is None or parsed is None:
            undated.append((None, entry))
        else:
            dated.append((parsed, created, entry))
    dated.sort(key=lambda item: item[0])
    return [(created, entry) for _, created, entry in dated] + undated


def _status_text(entry: dict, descriptions: dict[str, str]) -> str | None:
    """Burd's own text for a status entry, else its status type."""
    status_type = entry.get("statusType")
    key = _status_key(status_type)
    if key and descriptions.get(key):
        return descriptions[key]
    return entry.get("displayText") or status_type or None


def build_history(
    statuses: list | None,
    descriptions: dict[str, str] | None = None,
    *,
    max_events: int = HISTORY_MAX_EVENTS,
) -> list[dict]:
    """Build the canonical ``history`` list from Burd's status list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. Sorted oldest → newest and capped to the
    most recent ``max_events``. Entries without a timestamp are left out.
    """
    history = [
        {
            "timestamp": created,
            "status": map_event_status(entry.get("statusType")),
            "raw_status": _status_text(entry, descriptions or {}),
        }
        for created, entry in _ordered_statuses(statuses)
        if created is not None
    ]
    return history[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the burd.dk tracking deep-link for a shipment."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def sender_name(customer_id: str | None) -> str | None:
    """Return a display name for Burd's webshop id (``proshop`` → ``Proshop``)."""
    if not isinstance(customer_id, str) or not customer_id.strip():
        return None
    key = customer_id.strip()
    known = _SENDER_NAMES.get(key.lower())
    if known:
        return known
    return " ".join(word[:1].upper() + word[1:] for word in key.split())


def is_final(raw: dict) -> tuple[bool, str | None]:
    """Whether the newest status is cancelled or returned, and when it was set."""
    ordered = _ordered_statuses(raw.get("statuses"))
    if not ordered:
        return False, None
    created, newest = ordered[-1]
    return _status_key(newest.get("statusType")) in FINAL_STATUS_TYPES, created


def normalize_parcel(
    raw: dict, *, tracking_code: str | None = None, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. ``raw`` is the dict
    :mod:`.api` assembled; a not-yet-known shipment is ``{}``.

    * The status is the newest entry's. An unmapped newest entry falls back to
      the newest one that maps, and only then to ``unknown``.
    * ``planned_from`` / ``planned_to`` are the Min levering window, only for
      a parcel added with that link, and cleared once the shipment is
      delivered, cancelled or returned.
    * ``receiver`` is always ``None``: Burd's only source for it is the
      Min levering reply, whose personal fields are never kept.
    """
    statuses = raw.get("statuses") or []
    descriptions = raw.get("descriptions") or {}
    ordered = _ordered_statuses(statuses)
    barcode = raw.get("number") or tracking_code

    status = ParcelStatus.UNKNOWN
    raw_status: str | None = None
    current_type: str | None = None
    if ordered:
        newest = ordered[-1][1]
        raw_status = _status_text(newest, descriptions)
        current_type = _status_key(newest.get("statusType"))
        status = map_parcel_status(newest.get("statusType"))
        if status is ParcelStatus.UNKNOWN:
            for _, entry in reversed(ordered[:-1]):
                mapped = _STATUS_MAP.get(_status_key(entry.get("statusType")) or "")
                if mapped is not None:
                    status = mapped
                    break
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        delivered_at = next(
            (
                created
                for created, entry in reversed(ordered)
                if _status_key(entry.get("statusType")) == "delivered"
            ),
            None,
        )

    window = raw.get("timeWindow") or {}
    planned_from = planned_to = None
    if not delivered and current_type not in FINAL_STATUS_TYPES:
        planned_from = to_utc_iso(window.get("start"), "timeWindow.start")
        planned_to = to_utc_iso(window.get("end"), "timeWindow.end")
        if (
            planned_from
            and planned_to
            and parse_iso(planned_to) == parse_iso(planned_from)
        ):
            planned_to = None

    customer_id = next(
        (e.get("customerId") for _, e in reversed(ordered) if e.get("customerId")),
        None,
    )

    return {
        "carrier": CARRIER_NAME,
        "barcode": barcode,
        "sender": raw.get("companyName") or sender_name(customer_id),
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(barcode),
        "weight": None,
        "dimensions": None,
        "history": build_history(statuses, descriptions) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
