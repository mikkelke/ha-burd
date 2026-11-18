"""Synthetic Burd payloads shared by the test modules.

Shaped after what Burd's own tracking pages read from the public endpoints.
Every number, token, order number and name here is made up.
"""

from __future__ import annotations

ACTIVE_CODE = "100000000000000001"
DELIVERED_CODE = "100000000000000002"
TOKEN = "Tk9fUmVhbFRva2VuMQ"
CUSTOMER_ID = "exampleshop"


def status(
    status_type: str,
    created: str,
    *,
    number: str = DELIVERED_CODE,
    customer_id: str = CUSTOMER_ID,
) -> dict:
    """One entry of Burd's status list."""
    return {
        "statusType": status_type,
        "created": created,
        "barcode": number,
        "orderNumber": "ORD-0001",
        "customerId": customer_id,
        "destinationType": "address",
    }


def labels_reply() -> list[dict]:
    """The labels endpoint's reply (status types spelled PascalCase)."""
    return [
        {"statusType": "Registered", "description": "Waiting for the webshop."},
        {"statusType": "Received", "description": "At our terminal."},
        {"statusType": "DeliveryOnTheWay", "description": "Out for delivery."},
        {"statusType": "Delivered", "description": "Delivered."},
    ]


def status_reply(code: str = DELIVERED_CODE) -> list[dict]:
    """The status endpoint's reply for a delivered shipment, newest first."""
    return [
        status("delivered", "2026-11-17T19:41:08Z", number=code),
        status("deliveryOnTheWay", "2026-11-17T16:02:44Z", number=code),
        status("received", "2026-11-17T09:15:00Z", number=code),
        status("registered", "2026-11-16T21:30:12Z", number=code),
    ]


def token_reply(code: str = ACTIVE_CODE) -> dict:
    """The Min levering reply, personal fields included as Burd sends them."""
    return {
        "barcode": code,
        "customerId": CUSTOMER_ID,
        "companyName": "Example Shop",
        "reschedulingExpired": False,
        "showDeliveryDate": True,
        "invalidAddress": False,
        "destination": {
            "address": "Testvej 1",
            "zipCode": "9999",
            "city": "Testby",
            "phone": {"number": "12345678"},
            "email": "someone@example.invalid",
            "name": "Test Person",
            "timeWindow": {
                "start": "2026-11-18T17:00:00Z",
                "end": "2026-11-18T21:00:00Z",
            },
        },
        "trackAndTraceStatuses": [],
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """The raw dict api.py assembles for a delivered shipment."""
    return {
        "number": code,
        "statuses": list(reversed(status_reply(code))),
        "descriptions": {
            "delivered": "Delivered.",
            "deliveryontheway": "Out for delivery.",
            "received": "At our terminal.",
            "registered": "Waiting for the webshop.",
        },
        "timeWindow": None,
        "companyName": None,
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery shipment added by its Min levering link."""
    sample = delivered_sample(code)
    sample["statuses"] = sample["statuses"][:-1]
    sample["timeWindow"] = {
        "start": "2026-11-18T17:00:00Z",
        "end": "2026-11-18T21:00:00Z",
    }
    sample["companyName"] = "Example Shop"
    return sample


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """Burd delivers to the door only; kept for the shared capability test."""
    return active_sample(code)
