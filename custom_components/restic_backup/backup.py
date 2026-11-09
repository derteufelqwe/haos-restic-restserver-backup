"""Backup platform for the Restic Backup integration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
)
from homeassistant.core import HomeAssistant, callback

from .client import (
    ResticBackupClient,
    ResticBackupClientError,
    ResticBackupNotFoundError,
)
from .const import DOMAIN, EVENT_TOAST

_LOGGER = logging.getLogger(__name__)

DATA_BACKUP_AGENT_LISTENERS = f"{DOMAIN}.backup_agent_listeners"


async def async_get_backup_agents(
    hass: HomeAssistant, **kwargs: Any
) -> list[BackupAgent]:
    """Return the restic backup agent for every configured entry."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return [ResticBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener to be called when agents are added/removed."""
    listeners: list[Callable[[], None]] = hass.data.setdefault(
        DATA_BACKUP_AGENT_LISTENERS, []
    )
    listeners.append(listener)

    @callback
    def _remove() -> None:
        listeners.remove(listener)

    return _remove


@callback
def notify_backup_listeners(hass: HomeAssistant) -> None:
    """Notify all registered listeners that the set of agents changed."""
    for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
        listener()


class ResticBackupAgent(BackupAgent):
    """Backup agent that stores backups through the restic-backup add-on."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self._hass = hass
        self.name = entry.title
        self.unique_id = entry.entry_id
        self._client: ResticBackupClient = entry.runtime_data

    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        try:
            stream = await self._client.download_backup(backup_id)
        except ResticBackupNotFoundError as err:
            raise BackupNotFound(backup_id) from err
        except ResticBackupClientError as err:
            self._show_error("Downloading the backup from restic failed", err)
            raise BackupAgentError(str(err)) from err
        return self._report_stream_errors(stream)

    async def _report_stream_errors(
        self, stream: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        """Pass the chunks on, and show a toast if the download breaks midway."""
        try:
            async for chunk in stream:
                yield chunk
        except ResticBackupClientError as err:
            self._show_error("Downloading the backup from restic failed", err)
            raise BackupAgentError(str(err)) from err

    @callback
    def _show_error(self, message: str, err: Exception) -> None:
        """Show an error toast. The details only go to the log: they can be long."""
        _LOGGER.error("%s: %s", message, err)
        self._hass.bus.async_fire(EVENT_TOAST, {"message": message})

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: Callable[..., None] | None = None,
        **kwargs: Any,
    ) -> None:
        def _progress(bytes_uploaded: int) -> None:
            # Older Home Assistant versions don't pass on_progress.
            if on_progress is not None:
                on_progress(bytes_uploaded=bytes_uploaded)

        try:
            await self._client.upload_backup(
                backup.backup_id, backup.as_dict(), open_stream, _progress
            )
        except ResticBackupClientError as err:
            raise BackupAgentError(str(err)) from err

        # The repository's disk is awake now, so this is the time to forget
        # backups that were pruned from it. A failure must not fail the backup.
        try:
            await self._client.sync()
        except ResticBackupClientError as err:
            _LOGGER.warning("Could not sync with the restic repository: %s", err)

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Refuse to delete: backups in this location are kept permanently."""
        # HA also calls this for backups that were never stored here. Those must
        # report BackupNotFound, or deleting a local-only backup would fail too.
        await self.async_get_backup(backup_id)
        # The frontend ignores the errors returned for a delete, so without a
        # toast (see js/toast.js) the refusal would be invisible.
        self._hass.bus.async_fire(
            EVENT_TOAST,
            {"message": "Deleting backups from this location is not supported"},
        )
        raise BackupAgentError("Deleting backups from this location is not supported")

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        try:
            entries = await self._client.list_backups()
        except ResticBackupClientError as err:
            raise BackupAgentError(str(err)) from err
        return [AgentBackup.from_dict(entry) for entry in entries]

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        try:
            entry = await self._client.get_backup(backup_id)
        except ResticBackupNotFoundError as err:
            raise BackupNotFound(backup_id) from err
        except ResticBackupClientError as err:
            raise BackupAgentError(str(err)) from err
        return AgentBackup.from_dict(entry)
