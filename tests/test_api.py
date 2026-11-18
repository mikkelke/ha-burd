"""Tests for the Burd API client."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.burd.api import BurdApiClient, BurdApiError

from .payloads import (
    ACTIVE_CODE,
    CUSTOMER_ID,
    DELIVERED_CODE,
    TOKEN,
    labels_reply,
    status_reply,
    token_reply,
)

PERSONAL = ("Testvej", "9999", "Testby", "12345678", "example.invalid", "Test Person")


def _response(status: int, body: object = None, headers: dict | None = None):
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(routes: dict[str, object]) -> MagicMock:
    """A session answering by URL fragment: ``{fragment: (status, body)}``.

    A value that is an exception is raised instead.
    """

    def _get(url, params=None):
        for fragment, answer in routes.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return _response(*answer)
        raise AssertionError(f"unexpected request to {url}")

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _routes(**overrides) -> dict[str, object]:
    routes: dict[str, object] = {
        "/Descriptions": (200, labels_reply()),
        "/TrackAndTraceStatuses": (200, status_reply(ACTIVE_CODE)),
        "/UIReschedulingInfo/": (200, token_reply(ACTIVE_CODE)),
    }
    routes.update(overrides)
    return routes


async def test_get_parcel_assembles_statuses_and_labels():
    session = _session(_routes())
    client = BurdApiClient(session, "en")

    raw = await client.async_get_parcel(ACTIVE_CODE)

    assert raw["number"] == ACTIVE_CODE
    assert [s["statusType"] for s in raw["statuses"]][0] == "delivered"
    assert raw["descriptions"]["deliveryontheway"] == "Out for delivery."
    assert raw["timeWindow"] is None
    assert raw["companyName"] is None
    status_call = session.get.call_args_list[0]
    assert status_call.kwargs["params"] == {"number": ACTIVE_CODE}
    labels_call = session.get.call_args_list[1]
    assert labels_call.kwargs["params"] == {"customerId": CUSTOMER_ID, "lang": "en"}


async def test_get_parcel_with_token_reads_only_the_window_and_shop_name():
    session = _session(_routes())
    client = BurdApiClient(session)

    raw = await client.async_get_parcel(ACTIVE_CODE, TOKEN)

    assert raw["timeWindow"] == {
        "start": "2026-11-18T17:00:00Z",
        "end": "2026-11-18T21:00:00Z",
    }
    assert raw["companyName"] == "Example Shop"
    assert not any(value in repr(raw) for value in PERSONAL)
    assert TOKEN not in repr(raw)
    assert any(TOKEN in call.args[0] for call in session.get.call_args_list)


async def test_get_parcel_ignores_a_token_for_another_shipment(caplog):
    client = BurdApiClient(
        _session(
            _routes(**{"/UIReschedulingInfo/": (200, token_reply(DELIVERED_CODE))})
        )
    )

    raw = await client.async_get_parcel(ACTIVE_CODE, TOKEN)

    assert raw["timeWindow"] is None
    assert "another shipment number" in caplog.text
    assert DELIVERED_CODE not in caplog.text


async def test_get_parcel_with_an_expired_token_has_no_window():
    expired = {"reschedulingExpired": True, "showDeliveryDate": False}
    client = BurdApiClient(
        _session(_routes(**{"/UIReschedulingInfo/": (200, expired)}))
    )

    raw = await client.async_get_parcel(ACTIVE_CODE, TOKEN)

    assert raw["timeWindow"] is None


async def test_get_parcel_returns_none_for_an_unknown_number():
    """Burd answers 400 with an empty body; a normal state, not an error."""
    client = BurdApiClient(_session(_routes(**{"/TrackAndTraceStatuses": (400, None)})))
    assert await client.async_get_parcel(ACTIVE_CODE) is None


async def test_get_parcel_returns_none_for_an_empty_list():
    client = BurdApiClient(_session(_routes(**{"/TrackAndTraceStatuses": (200, [])})))
    assert await client.async_get_parcel(ACTIVE_CODE) is None


async def test_statuses_accept_an_events_envelope():
    client = BurdApiClient(
        _session(
            _routes(
                **{
                    "/TrackAndTraceStatuses": (
                        200,
                        {"events": status_reply(ACTIVE_CODE)},
                    )
                }
            )
        )
    )
    raw = await client.async_get_parcel(ACTIVE_CODE)
    assert len(raw["statuses"]) == 4


async def test_unknown_status_fields_are_dropped_and_named_once(caplog):
    reply = status_reply(ACTIVE_CODE)
    for entry in reply:
        entry["driverPosition"] = {"lat": 55.123, "lon": 12.456}
    reply.append("not-an-object")
    client = BurdApiClient(
        _session(_routes(**{"/TrackAndTraceStatuses": (200, reply)}))
    )

    raw = await client.async_get_parcel(ACTIVE_CODE)

    assert all("driverPosition" not in entry for entry in raw["statuses"])
    assert caplog.text.count("driverPosition") == 1
    assert "a status entry is not an object" in caplog.text
    assert "55.123" not in caplog.text


@pytest.mark.parametrize("body", ["not json", {"unexpected": "object"}])
async def test_statuses_raise_on_an_unexpected_body(body):
    client = BurdApiClient(_session(_routes(**{"/TrackAndTraceStatuses": (200, body)})))
    with pytest.raises(BurdApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_statuses_raise_on_a_server_error():
    client = BurdApiClient(_session(_routes(**{"/TrackAndTraceStatuses": (500, None)})))
    with pytest.raises(BurdApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 500


@pytest.mark.parametrize(("header", "expected"), [("120", 120.0), ("soon", None)])
async def test_429_carries_retry_after(header, expected):
    client = BurdApiClient(
        _session(
            _routes(**{"/TrackAndTraceStatuses": (429, None, {"Retry-After": header})})
        )
    )
    with pytest.raises(BurdApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == expected


async def test_status_network_error_propagates():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    client = BurdApiClient(
        _session(_routes(**{"/TrackAndTraceStatuses": aiohttp.ClientError("boom")}))
    )
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_token_network_error_hides_the_url():
    """aiohttp's message can carry the URL, and the token is part of it."""
    client = BurdApiClient(
        _session(
            _routes(
                **{"/UIReschedulingInfo/": aiohttp.ClientError(f"failed for /{TOKEN}")}
            )
        )
    )
    with pytest.raises(BurdApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE, TOKEN)
    assert TOKEN not in str(err.value)
    assert err.value.__cause__ is None


async def test_resolve_token_returns_barcode_window_and_shop():
    client = BurdApiClient(_session(_routes()))
    info = await client.async_resolve_token(TOKEN)
    assert info.barcode == ACTIVE_CODE
    assert info.company_name == "Example Shop"
    assert info.time_window["start"] == "2026-11-18T17:00:00Z"


async def test_resolve_token_without_a_window():
    reply = token_reply()
    del reply["destination"]
    reply["companyName"] = ""
    client = BurdApiClient(_session(_routes(**{"/UIReschedulingInfo/": (200, reply)})))
    info = await client.async_resolve_token(TOKEN)
    assert info.time_window is None
    assert info.company_name is None


async def test_resolve_token_drops_unknown_window_fields(caplog):
    reply = token_reply()
    reply["destination"]["timeWindow"]["slot"] = "A"
    client = BurdApiClient(_session(_routes(**{"/UIReschedulingInfo/": (200, reply)})))
    info = await client.async_resolve_token(TOKEN)
    assert set(info.time_window) == {"start", "end"}
    assert "timeWindow fields not handled yet (dropped): slot" in caplog.text


async def test_resolve_token_ignores_a_window_that_is_not_an_object(caplog):
    reply = token_reply()
    reply["destination"]["timeWindow"] = "tonight"
    client = BurdApiClient(_session(_routes(**{"/UIReschedulingInfo/": (200, reply)})))
    info = await client.async_resolve_token(TOKEN)
    assert info.time_window is None
    assert "timeWindow is not an object" in caplog.text


@pytest.mark.parametrize("answer", [(400, None), (200, {"reschedulingExpired": True})])
async def test_resolve_token_unknown_is_none(answer):
    client = BurdApiClient(_session(_routes(**{"/UIReschedulingInfo/": answer})))
    assert await client.async_resolve_token(TOKEN) is None


async def test_resolve_token_rejects_a_non_object():
    client = BurdApiClient(_session(_routes(**{"/UIReschedulingInfo/": (200, [])})))
    with pytest.raises(BurdApiError):
        await client.async_resolve_token(TOKEN)


async def test_labels_are_cached_per_shop():
    session = _session(_routes())
    client = BurdApiClient(session, "da")

    await client.async_get_labels(CUSTOMER_ID)
    labels = await client.async_get_labels(CUSTOMER_ID)

    assert labels["registered"] == "Waiting for the webshop."
    calls = [c for c in session.get.call_args_list if "/Descriptions" in c.args[0]]
    assert len(calls) == 1
    assert calls[0].kwargs["params"]["lang"] == "da"


async def test_labels_failure_is_not_cached_and_falls_back_to_nothing():
    session = _session(_routes(**{"/Descriptions": (500, None)}))
    client = BurdApiClient(session)

    assert await client.async_get_labels(CUSTOMER_ID) == {}
    assert await client.async_get_labels(CUSTOMER_ID) == {}
    calls = [c for c in session.get.call_args_list if "/Descriptions" in c.args[0]]
    assert len(calls) == 2


async def test_labels_skip_malformed_rows():
    reply = [*labels_reply(), "row", {"statusType": "Odd"}, {"description": "x"}]
    client = BurdApiClient(_session(_routes(**{"/Descriptions": (200, reply)})))
    labels = await client.async_get_labels(CUSTOMER_ID)
    assert set(labels) == {"registered", "received", "deliveryontheway", "delivered"}
