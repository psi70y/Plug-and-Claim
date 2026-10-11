"""Sensors for Plug & Claim.

Energy and claim sensors for this month (including today so far), last month
and the month picked in the "Report month" select, plus two diagnostics.
Everything comes from the coordinator's snapshot, so values match the report.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CURRENCY
from .coordinator import MonthSummary, PlugAndClaimConfigEntry, PlugAndClaimData
from .download import async_signed_url
from .entity import PlugAndClaimEntity

# The coordinator pushes updates; no entity polls on its own.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MonthSensorDescription(SensorEntityDescription):
    """A sensor showing one figure of one month's summary."""

    month: Callable[[PlugAndClaimData], MonthSummary | None]
    value: Callable[[MonthSummary], Decimal]
    detailed: bool = False  # add rate/days/report attributes


def _energy(key: str, month, detailed: bool = False) -> MonthSensorDescription:
    return MonthSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        month=month,
        value=lambda summary: summary.kwh,
        detailed=detailed,
    )


def _claim(key: str, month, detailed: bool = False) -> MonthSensorDescription:
    return MonthSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        month=month,
        value=lambda summary: summary.amount,
        detailed=detailed,
    )


MONTH_SENSORS: tuple[MonthSensorDescription, ...] = (
    _energy("this_month_energy", lambda d: d.this_month),
    _claim("this_month_claim", lambda d: d.this_month),
    _energy("last_month_energy", lambda d: d.last_month),
    _claim("last_month_claim", lambda d: d.last_month),
    _energy("selected_month_energy", lambda d: d.selected, detailed=True),
    _claim("selected_month_claim", lambda d: d.selected, detailed=True),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlugAndClaimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        *(MonthSensor(coordinator, description) for description in MONTH_SENSORS),
        LastSyncSensor(coordinator, "last_sync"),
        LedgerDaysSensor(coordinator, "ledger_days"),
    ])


class MonthSensor(PlugAndClaimEntity, SensorEntity):
    """kWh or claim amount for this month, last month or the selected month."""

    entity_description: MonthSensorDescription
    # Signed download links are temporary keys: keep them out of history.
    _unrecorded_attributes = frozenset({"pdf_url", "xlsx_url"})

    def __init__(self, coordinator, description: MonthSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def _summary(self) -> MonthSummary | None:
        return self.entity_description.month(self.coordinator.data)

    @property
    def native_value(self) -> Decimal | None:
        summary = self._summary
        return self.entity_description.value(summary) if summary else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        summary = self._summary
        if summary is None:
            return None
        attributes: dict[str, Any] = {
            "month": summary.month,
            "rate": summary.rate,
            "day_count": summary.day_count,
            "missing_days": summary.missing_days,
        }
        if self.entity_description.detailed:
            report = summary.report or {}
            attributes["report_files"] = report.get("files", [])
            attributes["report_generated"] = report.get("generated")
            attributes["report_amount"] = report.get("amount")
            # Signed links for dashboards, e.g. a markdown card (see README).
            entry_id = self.coordinator.config_entry.entry_id
            for file in report.get("files", []):
                suffix = file.rsplit(".", 1)[-1]
                if suffix in ("pdf", "xlsx"):
                    attributes[f"{suffix}_url"] = async_signed_url(self.hass, entry_id, file)
        return attributes


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
