"""The Plug & Claim integration.

Keeps a daily kWh ledger for an energy sensor and produces monthly claim
reports. Setup is UI-only (config entries).
"""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .coordinator import PlugAndClaimConfigEntry, PlugAndClaimCoordinator
from .download import ReportDownloadView
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _ensure_default_log_level() -> None:
    """Default this integration's loggers to INFO so sync and report events
    show in the log out of the box.

    Only applies if the user hasn't set a level themselves (NOTSET means
    "never touched, just inheriting"), so a `logger:` override in
    configuration.yaml - e.g. debug, or silencing it - is always respected.
    Child loggers (plug_and_claim.coordinator etc.) inherit this level.
    """
    logger = logging.getLogger(__name__)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration-wide parts: logging default and actions."""
    _ensure_default_log_level()
    async_setup_services(hass)
    hass.http.register_view(ReportDownloadView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> bool:
    """Set up one claim from a config entry."""
    coordinator = PlugAndClaimCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Startup sync (once HA has fully started) and the nightly 00:10 sync.
    coordinator.async_start()
    # Reload when options change so new settings take effect straight away.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Set up claim '%s' for %s", entry.title, entry.data.get("energy_sensor"))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> bool:
    """Unload a claim. Listeners registered with async_on_unload are removed by HA."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> None:
    """Claim deleted: clear its Repairs issues.

    The ledger file is deliberately kept in /config/.storage, so a claim
    deleted by mistake doesn't lose its history.
    """
    issue_registry = ir.async_get(hass)
    prefix = f"missing_days_{entry.entry_id}_"
    for domain, issue_id in list(issue_registry.issues):
        if domain == DOMAIN and issue_id.startswith(prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def _async_update_listener(hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
