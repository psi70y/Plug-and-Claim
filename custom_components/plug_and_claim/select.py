"""The "Report month" select: picks the month the Selected month sensors and
the Generate report button work on. The choice is saved in the ledger, so it
survives restarts."""

from datetime import date

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PlugAndClaimConfigEntry
from .entity import PlugAndClaimEntity

PARALLEL_UPDATES = 0


def month_label(month: str) -> str:
    """'2026-09' -> '2026-09 (Sep 2026)'."""
    return f"{month} ({date.fromisoformat(f'{month}-01'):%b %Y})"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlugAndClaimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ReportMonthSelect(entry.runtime_data, "report_month")])


class ReportMonthSelect(PlugAndClaimEntity, SelectEntity):
    """Finished months in the ledger, newest first."""

    @property
    def options(self) -> list[str]:
        return [month_label(m) for m in self.coordinator.data.report_months]

    @property
    def current_option(self) -> str | None:
        selected = self.coordinator.data.selected
        return month_label(selected.month) if selected else None

    async def async_select_option(self, option: str) -> None:
        # The label starts with the YYYY-MM month key.
        await self.coordinator.async_select_month(option[:7])
