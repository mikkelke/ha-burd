"""Config flow for the Burd parcel tracker integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import BurdApiClient, BurdApiError
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TOKEN,
    CONF_TRACKING_CODE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_LINK_HOST = "k.burd.dk"
_TRACK_HOST = "burd.dk"
_TOKEN_SEPARATOR = "/"
# Min levering routes whose first segment is a prefix, not the token.
_LINK_PREFIXES = frozenset({"f", "e", "web"})
_NUMBER_LIKE = re.compile(r"[\d\s-]+")


class InvalidTrackingInput(ValueError):
    """A pasted value is not a usable number, number + token, or Burd link.

    ``reason`` is the translation key of the error shown to the user.
    """

    def __init__(self, reason: str) -> None:
        """Store the error's translation key."""
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TrackingInput:
    """What one pasted line holds before it is resolved.

    ``token_only`` marks a bare value that is not a number: it is tried as a
    Min levering token first and kept as a shipment number when Burd does not
    know it as one.
    """

    number: str | None
    token: str | None
    token_only: bool = False


def normalize_tracking_code(value: str | None) -> str:
    """Return the shipment number upper-cased with separators stripped."""
    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())


def normalize_token(value: str | None) -> str | None:
    """Return the token trimmed — never re-cased, Burd compares it exactly."""
    token = (value or "").strip()
    return token or None


def valid_tracking_code(value: str) -> bool:
    """Accept every non-empty code.

    Carriers' real tracking-number formats vary too much, and often aren't
    fully confirmed, to gate on a guessed shape — a false negative from a
    too-strict regex is far more annoying than a bad code that simply comes
    back "not found" on the next poll. Do not add a format regex here; this
    is a suite-wide convention, not a per-carrier TODO.
    """
    return bool(value)


def _parse_link(text: str) -> TrackingInput:
    url = urlsplit(text if "://" in text else f"https://{text}")
    host = (url.hostname or "").lower().removeprefix("www.")
    segments = [part for part in url.path.split("/") if part]
    if host == _LINK_HOST and segments:
        if segments[0].lower() in _LINK_PREFIXES:
            segments = segments[1:]
        token = normalize_token(segments[0]) if segments else None
        if token:
            return TrackingInput(number=None, token=token)
    if host == _TRACK_HOST and segments[:1] == ["track"]:
        code = normalize_tracking_code(parse_qs(url.query).get("code", [""])[0])
        if valid_tracking_code(code):
            return TrackingInput(number=code, token=None)
    raise InvalidTrackingInput("unsupported_link")


def parse_tracking_input(value: str | None) -> TrackingInput:
    """Split a Burd link, ``NUMBER/TOKEN``, a number or a bare token into parts.

    Links: the recipient's Min levering link (``https://k.burd.dk/f/<token>``,
    also without ``f/``) and the burd.dk tracking page (``…/track?code=<n>``).
    """
    text = (value or "").strip()
    if "://" in text or text.lower().startswith((_LINK_HOST, _TRACK_HOST, "www.")):
        return _parse_link(text)
    if _TOKEN_SEPARATOR in text:
        number, _, token = text.partition(_TOKEN_SEPARATOR)
        code = normalize_tracking_code(number)
        if not valid_tracking_code(code) or not normalize_token(token):
            raise InvalidTrackingInput("invalid_tracking_code")
        return TrackingInput(number=code, token=normalize_token(token))
    if _NUMBER_LIKE.fullmatch(text):
        return TrackingInput(number=normalize_tracking_code(text), token=None)
    if not text or any(char.isspace() for char in text):
        raise InvalidTrackingInput("invalid_tracking_code")
    return TrackingInput(number=None, token=text, token_only=True)


async def async_resolve_input(
    hass: HomeAssistant, parsed: TrackingInput
) -> tuple[str, str | None]:
    """Return the ``(number, token)`` to store for one parsed line.

    A token is looked up once to learn its shipment number, so a link that
    does not resolve is refused here instead of tracking nothing. A number on
    its own is never looked up: Burd answers "unknown" until the webshop has
    registered the shipment, which is no reason to refuse it.
    """
    if parsed.token is None:
        if not parsed.number:
            raise InvalidTrackingInput("invalid_tracking_code")
        return parsed.number, None
    client = BurdApiClient(async_get_clientsession(hass))
    try:
        info = await client.async_resolve_token(parsed.token)
    except BurdApiError as err:
        raise InvalidTrackingInput("cannot_connect") from err
    if info is None:
        if parsed.token_only:
            code = normalize_tracking_code(parsed.token)
            if valid_tracking_code(code):
                return code, None
        raise InvalidTrackingInput("not_recognised")
    barcode = normalize_tracking_code(info.barcode)
    if parsed.number is not None and parsed.number != barcode:
        raise InvalidTrackingInput("token_mismatch")
    return barcode, parsed.token


async def async_resolve_known(
    hass: HomeAssistant, parsed: TrackingInput, saved: list[dict[str, str]]
) -> tuple[str, str | None]:
    """Resolve like :func:`async_resolve_input`, answering from ``saved`` first.

    A token that is already saved needs no lookup, so editing the list or
    removing a parcel keeps working after its Min levering link has expired.
    """
    for parcel in saved:
        code = parcel[CONF_TRACKING_CODE]
        if (
            parsed.token is not None
            and parcel.get(CONF_TOKEN) == parsed.token
            and parsed.number in (None, code)
        ):
            return code, parsed.token
    return await async_resolve_input(hass, parsed)


def parcel_entry(code: str, token: str | None) -> dict[str, str]:
    """Return the stored form of one tracked shipment."""
    entry = {CONF_TRACKING_CODE: code}
    if token:
        entry[CONF_TOKEN] = token
    return entry


def display_entry(parcel: dict[str, str]) -> str:
    """Return the ``NUMBER`` or ``NUMBER/TOKEN`` line the parcels form shows."""
    token = parcel.get(CONF_TOKEN)
    code = parcel[CONF_TRACKING_CODE]
    return f"{code}{_TOKEN_SEPARATOR}{token}" if token else code


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


class BurdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Burd integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> BurdOptionsFlowHandler:
        """Return the options flow handler."""
        return BurdOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the Burd hub — single instance, no input needed.

        Tracking is keyed on the shipment number alone (no account, no postal
        code), so there is nothing to ask at setup: the entry is created
        straight away and parcels are added afterwards via the options flow,
        the ``burd.track_parcel`` service or a dashboard button.
        ``single_config_entry`` in the manifest enforces one hub.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Burd",
            data={},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )


class BurdOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels separately from integration settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        return self.async_show_menu(
            step_id="init", menu_options=["parcels", "settings"]
        )

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        current = _current_parcels(self.config_entry)
        lines = [display_entry(parcel) for parcel in current]
        if user_input is not None:
            lines = [
                line.strip()
                for line in user_input.get("tracking_codes") or []
                if line and line.strip()
            ]
            parcels: dict[str, dict[str, str]] = {}
            for line in lines:
                try:
                    code, token = await async_resolve_known(
                        self.hass, parse_tracking_input(line), current
                    )
                except InvalidTrackingInput as err:
                    errors["base"] = err.reason
                    break
                if code not in parcels or token:
                    parcels[code] = parcel_entry(code, token)
            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: list(parcels.values()),
                    },
                )
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": lines}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_PARCELS: _current_parcels(self.config_entry),
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
