"""Buttons: generate the report for the selected month, and sync now."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import PlugAndClaimConfigEntry, ReportError
from .entity import PlugAndClaimEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlugAndClaimConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([
        GenerateReportButton(coordinator, "generate_report"),
        SyncNowButton(coordinator, "sync_now"),
    ])


class GenerateReportButton(PlugAndClaimEntity, ButtonEntity):
    """Writes the report for the month chosen in "Report month"."""

    async def async_press(self) -> None:
        selected = self.coordinator.data.selected
        if selected is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_month_selected")
        try:
            await self.coordinator.async_generate_report(selected.month)
        except ReportError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=err.key,
                translation_placeholders=err.placeholders,
            ) from err


class SyncNowButton(PlugAndClaimEntity, ButtonEntity):
    """Re-reads the last 14 days from the recorder."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        await self.coordinator.async_sync(days_back=14)
