"""Config flow for the Restic Backup integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .addon import (
    AddonError,
    AddonNotInstalledError,
    AddonNotRunningError,
    async_get_addon_url,
)
from .client import ResticBackupClient, ResticBackupClientError
from .const import (
    CONF_PASSWORD,
    CONF_REPO_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        vol.Required(CONF_REPO_PASSWORD): _PASSWORD_SELECTOR,
    }
)


class ResticBackupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a restic REST server repository."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the rest-server's connection details and the repo password."""
        errors: dict[str, str] = {}

        # Nothing can be set up without the add-on, so say so before asking anything.
        try:
            addon_url = await async_get_addon_url(self.hass, require_running=True)
        except AddonNotInstalledError:
            return self.async_abort(reason="addon_not_installed")
        except AddonNotRunningError:
            return self.async_abort(reason="addon_not_running")
        except AddonError as err:
            _LOGGER.error("%s", err)
            return self.async_abort(reason="addon_unavailable")

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = ResticBackupClient(
                session,
                addon_url,
                user_input[CONF_URL],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_REPO_PASSWORD],
            )
            try:
                await client.verify()
            except ResticBackupClientError as err:
                _LOGGER.error("Could not connect to the restic repository: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Restic Backup", data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
