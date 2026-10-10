"""Sensors for Plug & Claim. So far only the diagnostic ones (Last sync,
Ledger days); the energy and claim sensors arrive in Phase 4."""

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
    async_add_entities([
        LastSyncSensor(coordinator, "last_sync"),
        LedgerDaysSensor(coordinator, "ledger_days"),
    ])


class LastSyncSensor(PlugAndClaimEntity, SensorEntity):
    """When the ledger was last synced."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.last_sync


class LedgerDaysSensor(PlugAndClaimEntity, SensorEntity):
    """How many days the ledger holds, plus how they are being captured."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "days"

    @property
    def native_value(self) -> int:
        return self.coordinator.data.ledger_days

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        ledger = self.coordinator.ledger
        return {
            "capture_mode": self.coordinator.data.mode,
            "first_day": min(ledger.days, default=None),
            "last_day": max(ledger.days, default=None),
        }
