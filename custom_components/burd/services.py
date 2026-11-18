"""Services for the Burd parcel tracker integration.

`burd.track_parcel` / `burd.untrack_parcel` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button
or an automation that reads Burd's e-mail can start tracking a parcel.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import (
    InvalidTrackingInput,
    TrackingInput,
    async_resolve_known,
    parcel_entry,
    parse_tracking_input,
)
from .const import CONF_PARCELS, CONF_TOKEN, CONF_TRACKING_CODE, DOMAIN

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})

_INPUT_ERRORS = {
    "invalid_tracking_code": "That is not a Burd shipment number or link",
    "unsupported_link": (
        "Only the Min levering link (https://k.burd.dk/f/…) and the burd.dk "
        "tracking link are supported"
    ),
    "not_recognised": "Burd does not recognise that Min levering link",
    "token_mismatch": "That Min levering link belongs to another shipment number",
}


def _resolve_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the single Burd hub, or raise when it is not set up."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Burd is not set up")
    return entries[0]


def _parse(value: str) -> TrackingInput:
    try:
        return parse_tracking_input(value)
    except InvalidTrackingInput as err:
        raise ServiceValidationError(_INPUT_ERRORS[err.reason]) from None


async def _resolve(
    hass: HomeAssistant, parsed: TrackingInput, saved: list[dict[str, str]]
) -> tuple[str, str | None]:
    try:
        return await async_resolve_known(hass, parsed, saved)
    except InvalidTrackingInput as err:
        if err.reason == "cannot_connect":
            raise HomeAssistantError("Could not reach Burd, try again later") from None
        raise ServiceValidationError(_INPUT_ERRORS[err.reason]) from None


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Burd services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        parsed = _parse(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        tracking_code, token = await _resolve(hass, parsed, parcels)

        existing = next(
            (p for p in parcels if p[CONF_TRACKING_CODE] == tracking_code), None
        )
        if existing is not None and (
            token is None or existing.get(CONF_TOKEN) == token
        ):
            # Already tracked. A call without a link never drops a saved
            # token; removing one is an explicit edit in the options flow.
            return
        parcels = [p for p in parcels if p[CONF_TRACKING_CODE] != tracking_code]
        parcels.append(parcel_entry(tracking_code, token))
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        parsed = _parse(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        current = entry.options.get(CONF_PARCELS, [])
        tracking_code, _ = await _resolve(hass, parsed, current)
        kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Burd services (single-entry integration, so on unload)."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
