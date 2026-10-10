"""Actions (services) for Plug & Claim.

Registered once in async_setup (not per entry), as HA recommends, and each
call names the claim with config_entry_id.
"""

from datetime import date

try:
    import probatio as vol
except ImportError:  # HA < 2026.9
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import PlugAndClaimConfigEntry, PlugAndClaimCoordinator

SERVICE_SYNC = "sync"
SERVICE_SET_DAY = "set_day"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DAYS_BACK = "days_back"
ATTR_DATE = "date"
ATTR_KWH = "kwh"

SYNC_SCHEMA = vol.Schema({
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_DAYS_BACK, default=14): vol.All(vol.Coerce(int), vol.Range(min=1, max=3660)),
})

SET_DAY_SCHEMA = vol.Schema({
    vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_DATE): cv.date,
    vol.Required(ATTR_KWH): vol.All(vol.Coerce(float), vol.Range(min=0)),
})


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> PlugAndClaimCoordinator:
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry: PlugAndClaimConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"name": entry.title},
        )
    return entry.runtime_data


async def _async_sync(call: ServiceCall) -> ServiceResponse:
    coordinator = _coordinator(call.hass, call)
    result = await coordinator.async_sync(days_back=call.data[ATTR_DAYS_BACK])
    if not call.return_response:
        return None
    return {
        "mode": result.mode,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "updated_days": result.written,
        "days_with_missing_hours": result.gaps,
    }


async def _async_set_day(call: ServiceCall) -> None:
    coordinator = _coordinator(call.hass, call)
    day: date = call.data[ATTR_DATE]
    if day >= dt_util.now().date():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="date_not_past",
            translation_placeholders={"date": day.isoformat()},
        )
    await coordinator.async_set_day(day, call.data[ATTR_KWH])


def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC, _async_sync, schema=SYNC_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_SET_DAY, _async_set_day, schema=SET_DAY_SCHEMA)
