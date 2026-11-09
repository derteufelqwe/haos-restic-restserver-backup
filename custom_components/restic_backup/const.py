"""Constants for the Restic Backup integration."""

import os

DOMAIN = "restic_backup"

# Fired to show an error toast; the event data holds the "message". The name is
# repeated in js/toast.js.
EVENT_TOAST = f"{DOMAIN}_toast"

# The restic REST server (https://github.com/restic/rest-server) that actually
# stores the repository. Entered by the user.
CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# The restic repository's own encryption password (RESTIC_PASSWORD).
# Unrelated to CONF_USERNAME/CONF_PASSWORD, which authenticate against the
# rest-server's HTTP layer, not the repository itself.
CONF_REPO_PASSWORD = "repo_password"

# The companion add-on's internal bridge API. Not user-facing. Its address is looked up
# from the Supervisor (see addon.py); scripts/develop sets the override instead, because
# the devcontainer has no Supervisor and runs the add-on itself.
ADDON_SLUG = "restic_backup"
ADDON_PORT = 8099
ADDON_URL_OVERRIDE = os.environ.get("RESTIC_BACKUP_ADDON_URL")
