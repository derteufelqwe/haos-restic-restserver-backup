"""The Restic Backup integration."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import ClientSession
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .addon import AddonError, async_get_addon_url
from .backup import notify_backup_listeners
from .client import ResticBackupClient, ResticBackupClientError
from .const import (
    CONF_PASSWORD,
    CONF_REPO_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

TOAST_JS_URL = f"/{DOMAIN}/toast.js"
TOAST_JS_PATH = Path(__file__).parent / "js" / "toast.js"

ResticBackupConfigEntry = ConfigEntry[ResticBackupClient]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Load the toast script into the frontend."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(TOAST_JS_URL, str(TOAST_JS_PATH), cache_headers=False)]
    )
    add_extra_js_url(hass, TOAST_JS_URL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ResticBackupConfigEntry) -> bool:
    """Set up a restic-backup config entry."""
    try:
        addon_url = await async_get_addon_url(hass)
    except AddonError as err:
        raise ConfigEntryNotReady(str(err)) from err

    session: ClientSession = async_get_clientsession(hass)
    client = ResticBackupClient(
        session,
        addon_url,
        entry.data[CONF_URL],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_REPO_PASSWORD],
    )

    # The repository is deliberately not contacted here: its disk may be spun down,
    # and every restart of Home Assistant would wake it. The config flow verified
    # the connection, and every later call reports its own errors.
    entry.runtime_data = client
    entry.async_on_unload(
        entry.async_on_state_change(lambda: notify_backup_listeners(hass))
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, _restore_empty_index(client), "restic_backup restore index"
    )
    return True


async def _restore_empty_index(client: ResticBackupClient) -> None:
    """Rebuild the add-on's index from the repository if the add-on has none.

    That is the case after reinstalling Home Assistant or the add-on. It's the only
    time the repository is read at startup; with backups in the index nothing happens.
    """
    try:
        if not await client.list_backups():
            await client.sync()
    except ResticBackupClientError as err:
        _LOGGER.warning("Could not restore the backup index from restic: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: ResticBackupConfigEntry) -> bool:
    """Unload a restic-backup config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
