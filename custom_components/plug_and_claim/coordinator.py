"""Coordinator for Plug & Claim.

Entities read everything they show from this coordinator. In Phase 1 it holds
no ledger yet - only the "last sync" time - so the entity plumbing can be
tested on its own. Phase 2 adds the ledger (Store) and the statistics sync,
and calls async_set_updated_data() after each sync instead of polling.
"""

from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type PlugAndClaimConfigEntry = ConfigEntry[PlugAndClaimCoordinator]


@dataclass
class PlugAndClaimData:
    """Snapshot of what the entities display."""

    last_sync: datetime | None = None


class PlugAndClaimCoordinator(DataUpdateCoordinator[PlugAndClaimData]):
    """Holds the claim's data. No update_interval: it is pushed, not polled."""

    config_entry: PlugAndClaimConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PlugAndClaimConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
        )

    async def _async_update_data(self) -> PlugAndClaimData:
        # Nothing to fetch yet; keeps the last known data on refresh.
        return self.data or PlugAndClaimData()
