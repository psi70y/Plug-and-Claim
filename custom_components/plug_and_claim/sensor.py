"""Sensors for Plug & Claim. Phase 1 has only the diagnostic "Last sync"."""

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PlugAndClaimConfigEntry
from .entity import PlugAndClaimEntity

# The coordinator pushes updates; no entity polls on its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlugAndClaimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([LastSyncSensor(coordinator, "last_sync")])


class LastSyncSensor(PlugAndClaimEntity, SensorEntity):
    """When the ledger was last synced. Unknown until the first sync (Phase 2)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.last_sync
