"""Burd public tracking API client.

The contract the coordinator relies on:

* ``async_get_parcel`` returns the raw per-parcel dict on success,
* returns ``None`` when Burd does not know the shipment number (yet), a
  normal, expected state and never an error,
* raises :class:`BurdApiError` for anything else, with ``status_code`` set on
  a non-2xx response and ``retry_after`` set when Burd's own ``Retry-After``
  header on a 429 could be parsed as seconds; the coordinator's backoff reads
  both,
* lets ``aiohttp.ClientError`` from the status call propagate untouched.

The raw dict is assembled here, not passed through: ``statuses`` is Burd's
status list with only the fields this integration knows, ``descriptions`` is
Burd's own text for the status types in it, and ``timeWindow`` /
``companyName`` come from the Min levering reply when the parcel was added
with that link. Everything else in the Min levering reply (the recipient's
name, address, phone and e-mail) is dropped before this module returns.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import aiohttp

from .const import LABELS_API_URL, STATUS_API_URL, TOKEN_API_URL
from .parcels import warn_once

_LOGGER = logging.getLogger(__name__)

# Fields of one status entry this integration has seen named by Burd's own
# pages. Any other field is dropped from ``raw`` and named once in a warning,
# so a new field (a driver's position, a photo link) never reaches events or
# the recorder before someone has looked at it.
KNOWN_STATUS_KEYS = frozenset(
    {
        "statusType",
        "created",
        "displayText",
        "barcode",
        "orderNumber",
        "customerId",
        "destinationType",
        "id",
        "year",
    }
)
KNOWN_WINDOW_KEYS = frozenset({"start", "end"})


@dataclass(frozen=True)
class BurdTokenInfo:
    """The non-personal part of a Min levering reply."""

    barcode: str
    time_window: dict[str, Any] | None
    company_name: str | None


class BurdApiError(Exception):
    """Raised when a Burd API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Burd API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None  # an HTTP-date, not seconds; the coordinator backs off itself


class BurdApiClient:
    """Client for Burd's public tracking endpoints. No authentication."""

    def __init__(self, session: aiohttp.ClientSession, language: str = "da") -> None:
        """Initialise the client with an aiohttp session and a label language."""
        self._session = session
        self._language = "da" if language.lower().startswith("da") else "en"
        self._labels: dict[str, dict[str, str]] = {}

    async def _get_json(
        self, url: str, params: dict[str, str] | None = None
    ) -> Any | None:
        """GET ``url`` and return the parsed body, or ``None`` on HTTP 400."""
        async with self._session.get(url, params=params) as response:
            if response.status == 429:
                raise BurdApiError(
                    "HTTP 429", status_code=429, retry_after=_retry_after(response)
                )
            if response.status == 400:
                return None
            if response.status != 200:
                raise BurdApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                return await response.json(content_type=None)
            except ValueError as err:
                raise BurdApiError("unparseable body") from err

    async def async_get_statuses(self, number: str) -> list[dict[str, Any]] | None:
        """Return the shipment's status list, or ``None`` for an unknown number."""
        payload = await self._get_json(STATUS_API_URL, {"number": number})
        if payload is None:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            payload = payload["events"]
        if not isinstance(payload, list):
            raise BurdApiError("unexpected body (not a status list)")
        statuses: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                warn_once("status_row", "a status entry is not an object")
                continue
            unknown = sorted(set(item) - KNOWN_STATUS_KEYS)
            if unknown:
                warn_once(
                    "status_keys:" + ",".join(unknown),
                    "status entry fields not handled yet (dropped): %s",
                    ", ".join(unknown),
                )
            statuses.append({k: v for k, v in item.items() if k in KNOWN_STATUS_KEYS})
        return statuses or None

    async def async_resolve_token(self, token: str) -> BurdTokenInfo | None:
        """Return the barcode and delivery window behind a Min levering token.

        ``None`` when Burd does not know the token or it has expired. Network
        errors are re-raised as :class:`BurdApiError` carrying only the error
        type, because aiohttp's own message includes the URL and the token is
        part of it.
        """
        try:
            payload = await self._get_json(TOKEN_API_URL.format(token=token))
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BurdApiError(type(err).__name__) from None
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise BurdApiError("unexpected body (not a JSON object)")
        barcode = payload.get("barcode")
        if not barcode:
            return None
        destination = payload.get("destination")
        window = (
            destination.get("timeWindow") if isinstance(destination, dict) else None
        )
        if window is not None and not isinstance(window, dict):
            warn_once("window_type", "timeWindow is not an object (ignored)")
            window = None
        if window:
            unknown = sorted(set(window) - KNOWN_WINDOW_KEYS)
            if unknown:
                warn_once(
                    "window_keys:" + ",".join(unknown),
                    "timeWindow fields not handled yet (dropped): %s",
                    ", ".join(unknown),
                )
            window = {k: v for k, v in window.items() if k in KNOWN_WINDOW_KEYS}
        company = payload.get("companyName")
        return BurdTokenInfo(
            barcode=str(barcode),
            time_window=window or None,
            company_name=company if isinstance(company, str) and company else None,
        )

    async def async_get_labels(self, customer_id: str) -> dict[str, str]:
        """Return Burd's own text per status type for this webshop.

        Keys are lower-cased status types (the list spells them camelCase,
        this endpoint PascalCase). Cached for the session; a failure returns
        an empty dict and is retried on the next poll.
        """
        cached = self._labels.get(customer_id)
        if cached is not None:
            return cached
        try:
            payload = await self._get_json(
                LABELS_API_URL, {"customerId": customer_id, "lang": self._language}
            )
        except (BurdApiError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Burd status labels unavailable: %s", err)
            return {}
        labels: dict[str, str] = {}
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            status_type = item.get("statusType")
            text = item.get("description")
            if isinstance(status_type, str) and isinstance(text, str) and text:
                labels[status_type.lower()] = text
        self._labels[customer_id] = labels
        return labels

    async def async_get_parcel(
        self, number: str, token: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch one shipment: its status list, labels and, with a token, its window."""
        statuses = await self.async_get_statuses(number)
        if statuses is None:
            return None
        raw: dict[str, Any] = {
            "number": number,
            "statuses": statuses,
            "descriptions": {},
            "timeWindow": None,
            "companyName": None,
        }
        customer_id = next(
            (s["customerId"] for s in statuses if isinstance(s.get("customerId"), str)),
            None,
        )
        if customer_id:
            labels = await self.async_get_labels(customer_id)
            present = {
                s["statusType"].lower()
                for s in statuses
                if isinstance(s.get("statusType"), str)
            }
            raw["descriptions"] = {k: v for k, v in labels.items() if k in present}
        if token:
            info = await self.async_resolve_token(token)
            if info is not None and info.barcode != number:
                warn_once(
                    "token_mismatch",
                    "a Min levering link points at another shipment number; "
                    "its delivery window is ignored",
                )
            elif info is not None:
                raw["timeWindow"] = info.time_window
                raw["companyName"] = info.company_name
        return raw
