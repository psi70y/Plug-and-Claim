"""Config flow for Plug & Claim.

Setup has two steps:
  1. "user"  - name the claim and choose the sensors it reads (entry.data).
  2. "claim" - rate, vehicle and report settings (entry.options).
Everything from step 2 can be changed later through the options flow;
the sensors from step 1 are fixed (delete and re-add the claim to change them,
since the ledger built from one sensor doesn't apply to another).
"""

from datetime import date
import logging
import re
from typing import Any

# HA 2026.9 replaced voluptuous with probatio, which keeps the same API.
# Fall back to voluptuous so the integration still loads on older versions.
try:
    import probatio as vol
except ImportError:  # HA < 2026.9
    import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_AUTO_GENERATE,
    CONF_EFFECTIVE_FROM,
    CONF_ENERGY_SENSOR,
    CONF_GENERATE_TIME,
    CONF_INCLUDE_HEADER,
    CONF_MONTH_SENSOR,
    CONF_NAME,
    CONF_NOTIFY_SERVICE,
    CONF_OUTPUT_FOLDER,
    CONF_POWER_SENSOR,
    CONF_RATE,
    CONF_RATE_HISTORY,
    CONF_REGISTRATION,
    CONF_VEHICLE,
    CURRENCY,
    DEFAULT_AUTO_GENERATE,
    DEFAULT_GENERATE_TIME,
    DEFAULT_INCLUDE_HEADER,
    DEFAULT_NAME,
    DEFAULT_RATE,
    DOMAIN,
    OUTPUT_FOLDER_ROOT,
    RATE_DECIMALS,
    TPLINK_MONTH_KEY,
    TPLINK_PLATFORM,
    TPLINK_POWER_KEY,
    TPLINK_TODAY_KEY,
)
from .rates import add_rate, describe

_LOGGER = logging.getLogger(__name__)

NOTIFY_DOMAIN = "notify"
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _sensor_selector(device_class: str) -> selector.EntitySelector:
    """Entity picker limited to sensors of one device class."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=Platform.SENSOR, device_class=device_class)
    )


def _text_selector() -> selector.TextSelector:
    return selector.TextSelector(selector.TextSelectorConfig())


def _source_schema() -> vol.Schema:
    """Step 1: claim name and the sensors it reads."""
    return vol.Schema({
        vol.Required(CONF_NAME, default=DEFAULT_NAME): _text_selector(),
        vol.Required(CONF_ENERGY_SENSOR): _sensor_selector("energy"),
        vol.Optional(CONF_POWER_SENSOR): _sensor_selector("power"),
        vol.Optional(CONF_MONTH_SENSOR): _sensor_selector("energy"),
    })


def _claim_schema() -> vol.Schema:
    """Step 2 and the options flow: rate, vehicle and report settings.

    Free-text fields are Optional without a default, so a value the user
    clears is really removed instead of springing back to the default.
    Current/default values are shown with add_suggested_values_to_schema.
    """
    return vol.Schema({
        vol.Required(CONF_RATE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                # "any" allows the 6 decimal places a rate can have; HA's
                # smallest numeric step is 0.001. Rounded in _clean_claim_input.
                step="any",
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement=f"{CURRENCY}/kWh",
            )
        ),
        vol.Optional(CONF_VEHICLE): _text_selector(),
        vol.Optional(CONF_REGISTRATION): _text_selector(),
        vol.Required(CONF_AUTO_GENERATE): selector.BooleanSelector(),
        vol.Required(CONF_GENERATE_TIME): selector.TimeSelector(),
        vol.Required(CONF_OUTPUT_FOLDER): _text_selector(),
        vol.Required(CONF_INCLUDE_HEADER): selector.BooleanSelector(),
        vol.Optional(CONF_NOTIFY_SERVICE): _text_selector(),
    })


def _default_claim_options(name: str) -> dict[str, Any]:
    return {
        CONF_RATE: DEFAULT_RATE,
        CONF_AUTO_GENERATE: DEFAULT_AUTO_GENERATE,
        CONF_GENERATE_TIME: DEFAULT_GENERATE_TIME,
        CONF_OUTPUT_FOLDER: f"{OUTPUT_FOLDER_ROOT}/{slugify(name)}",
        CONF_INCLUDE_HEADER: DEFAULT_INCLUDE_HEADER,
    }


def _clean_claim_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Trim text fields, drop empty optional ones and tidy the folder path."""
    cleaned: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, str):
            value = value.strip()
            if key == CONF_OUTPUT_FOLDER:
                value = value.strip("/")
            if not value:
                continue
        elif key == CONF_RATE:
            value = round(float(value), RATE_DECIMALS)
        cleaned[key] = value
    return cleaned


def _validate_claim(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Check step 2 values; returns form errors keyed by field."""
    errors: dict[str, str] = {}

    if data[CONF_RATE] <= 0:
        errors[CONF_RATE] = "invalid_rate"

    # The folder must stay inside HA's media folder.
    folder = data.get(CONF_OUTPUT_FOLDER, "")
    parts = folder.split("/")
    if not folder or "\\" in folder or any(part in ("", ".", "..") for part in parts):
        errors[CONF_OUTPUT_FOLDER] = "invalid_folder"

    notify = data.get(CONF_NOTIFY_SERVICE)
    if notify:
        domain, _, service = notify.partition(".")
        if domain != NOTIFY_DOMAIN or not hass.services.has_service(domain, service):
            errors[CONF_NOTIFY_SERVICE] = "notify_not_found"

    return errors


@callback
def _suggest_sensors(hass: HomeAssistant) -> dict[str, str]:
    """Pre-fill the sensors from the first TP-Link plug that has a
    "Today's consumption" sensor, if there is one. Purely a convenience."""
    registry = er.async_get(hass)
    tplink = [
        entry
        for entry in registry.entities.values()
        if entry.platform == TPLINK_PLATFORM
        and entry.domain == Platform.SENSOR
        and entry.disabled_by is None
    ]
    today = next((e for e in tplink if e.translation_key == TPLINK_TODAY_KEY), None)
    if today is None:
        return {}

    suggested = {CONF_ENERGY_SENSOR: today.entity_id}
    for key, conf in ((TPLINK_POWER_KEY, CONF_POWER_SENSOR), (TPLINK_MONTH_KEY, CONF_MONTH_SENSOR)):
        sibling = next(
            (e for e in tplink if e.device_id == today.device_id and e.translation_key == key),
            None,
        )
        if sibling is not None:
            suggested[conf] = sibling.entity_id
    return suggested


class PlugAndClaimConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two-step setup: source sensors, then claim details."""

    VERSION = 1

    def __init__(self) -> None:
        self._source: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # One claim per energy sensor - a second one would just duplicate it.
            self._async_abort_entries_match({CONF_ENERGY_SENSOR: user_input[CONF_ENERGY_SENSOR]})
            user_input[CONF_NAME] = user_input[CONF_NAME].strip() or DEFAULT_NAME
            self._source = user_input
            return await self.async_step_claim()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _source_schema(), _suggest_sensors(self.hass)
            ),
        )

    async def async_step_claim(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        name = self._source[CONF_NAME]
        suggested: dict[str, Any] = _default_claim_options(name)

        if user_input is not None:
            options = _clean_claim_input(user_input)
            errors = _validate_claim(self.hass, options)
            if not errors:
                return self.async_create_entry(title=name, data=self._source, options=options)
            suggested = user_input

        return self.async_show_form(
            step_id="claim",
            data_schema=self.add_suggested_values_to_schema(_claim_schema(), suggested),
            errors=errors,
            description_placeholders={"name": name},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PlugAndClaimOptionsFlow()


class PlugAndClaimOptionsFlow(OptionsFlow):
    """Edit the claim details from step 2. HA provides self.config_entry.

    Changing the rate adds a second step asking which month the new rate
    applies from, so earlier months keep the rate they had (rate history).
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options
        suggested: dict[str, Any] = {**_default_claim_options(self.config_entry.title), **current}

        if user_input is not None:
            options = _clean_claim_input(user_input)
            errors = _validate_claim(self.hass, options)
            if not errors:
                # Replace (not merge) so cleared optional fields are removed,
                # but keep the rate history, which isn't on this form.
                if CONF_RATE_HISTORY in current:
                    options[CONF_RATE_HISTORY] = current[CONF_RATE_HISTORY]
                self._options = options
                if options[CONF_RATE] != current.get(CONF_RATE, options[CONF_RATE]):
                    return await self.async_step_rate_from()
                return self.async_create_entry(data=options)
            suggested = user_input

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(_claim_schema(), suggested),
            errors=errors,
            description_placeholders={
                "rates": describe(current.get(CONF_RATE_HISTORY), current.get(CONF_RATE, DEFAULT_RATE))
            },
        )

    async def async_step_rate_from(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask from which month the new rate applies."""
        errors: dict[str, str] = {}
        current = self.config_entry.options
        old_rate = current.get(CONF_RATE, DEFAULT_RATE)
        new_rate = self._options[CONF_RATE]

        if user_input is not None:
            month = user_input[CONF_EFFECTIVE_FROM].strip()
            if not MONTH_PATTERN.match(month):
                errors[CONF_EFFECTIVE_FROM] = "invalid_month"
            else:
                self._options[CONF_RATE_HISTORY] = add_rate(
                    current.get(CONF_RATE_HISTORY), old_rate, new_rate, month
                )
                return self.async_create_entry(data=self._options)

        this_month = dt_util.now().date().strftime("%Y-%m")
        return self.async_show_form(
            step_id="rate_from",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_EFFECTIVE_FROM): _text_selector()}),
                {CONF_EFFECTIVE_FROM: this_month},
            ),
            errors=errors,
            description_placeholders={
                "old_rate": f"{old_rate:.6f}",
                "new_rate": f"{new_rate:.6f}",
                "rates": describe(current.get(CONF_RATE_HISTORY), old_rate),
            },
        )
