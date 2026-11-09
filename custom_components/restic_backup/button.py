"""Button platform for the Restic Backup integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ResticBackupConfigEntry
from .client import ResticBackupClientError
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ResticBackupConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the sync button."""
    async_add_entities([SyncButton(entry)])


class SyncButton(ButtonEntity):
    """Forgets backups that were removed from the repository, on demand."""

    _attr_has_entity_name = True
    _attr_name = "Sync with repository"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ResticBackupConfigEntry) -> None:
        self._client = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_sync"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Compare the add-on's index with the repository. This wakes the repository's disk."""
        try:
            await self._client.sync()
        except ResticBackupClientError as err:
            raise HomeAssistantError(
                f"Could not sync with the restic repository: {err}"
            ) from err
