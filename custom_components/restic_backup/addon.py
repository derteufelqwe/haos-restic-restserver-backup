"""Finding the companion Restic Backup add-on.

An add-on installed from a Git repository gets the repository's hash in front of its
slug (and in its hostname, e.g. `a1b2c3d4-restic-backup`), a local add-on gets
`local_`. So the address can't be hardcoded: the Supervisor is asked for it.
"""

from __future__ import annotations

import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio

from .const import ADDON_PORT, ADDON_SLUG, ADDON_URL_OVERRIDE

# "<8 hex digit repository hash>_restic_backup", or "local_restic_backup".
_SLUG_PATTERN = re.compile(rf"^(?:local|[0-9a-f]{{8}})_{ADDON_SLUG}$")


class AddonError(Exception):
    """The add-on's address could not be determined."""


class AddonNotInstalledError(AddonError):
    """The add-on is not installed (or there is no Supervisor at all)."""


class AddonNotRunningError(AddonError):
    """The add-on is installed but stopped."""


async def async_get_addon_url(hass: HomeAssistant, *, require_running: bool = False) -> str:
    """Return the add-on's base URL, as reachable from Home Assistant."""
    if ADDON_URL_OVERRIDE:  # the development container runs the add-on itself
        return ADDON_URL_OVERRIDE
    if not is_hassio(hass):
        raise AddonNotInstalledError("There is no Supervisor, so no add-on can be installed")

    # Imported here: only available (and needed) when a Supervisor is present.
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import AddonState
    from homeassistant.components.hassio.handler import get_supervisor_client

    client = get_supervisor_client(hass)
    try:
        slug = next(
            (a.slug for a in await client.addons.list() if _SLUG_PATTERN.match(a.slug)),
            None,
        )
        if slug is None:
            raise AddonNotInstalledError("The Restic Backup add-on is not installed")
        info = await client.addons.addon_info(slug)
    except SupervisorError as err:
        raise AddonError(f"Cannot ask the Supervisor about the add-on: {err}") from err

    if require_running and info.state != AddonState.STARTED:
        raise AddonNotRunningError("The Restic Backup add-on is not running")
    return f"http://{info.hostname}:{ADDON_PORT}"
