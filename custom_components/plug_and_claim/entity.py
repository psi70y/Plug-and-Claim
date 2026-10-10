"""Shared entity base: puts every entity on the claim's device."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PlugAndClaimCoordinator


class PlugAndClaimEntity(CoordinatorEntity[PlugAndClaimCoordinator]):
    """Base for all Plug & Claim entities. One device per config entry."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PlugAndClaimCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Plug & Claim",
            model="Novated lease charging claim",
            entry_type=DeviceEntryType.SERVICE,
        )
