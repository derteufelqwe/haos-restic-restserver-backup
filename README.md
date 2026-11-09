# haos-restic-backups

Makes a restic repository selectable as a backup Location in Home Assistant's
Settings → System → Backups page, alongside "This system" and HA Cloud.

Two pieces:

- `custom_components/restic_backup` — a Core integration implementing the
  `BackupAgent` platform. This is what makes the Location show up in the
  picker, and it's where you enter your restic connection details (there's no
  separate add-on configuration step). It holds no restic logic itself; it
  forwards backup bytes, plus your connection details, to:
- `addon/restic_backup` — a Supervisor add-on with no configuration of its own.
  It exposes a small HTTP API (`/verify`, `/backups`, `/backups/{id}/upload`,
  `/backups/{id}/download`, `/backups/{id}`) that the integration calls into,
  building a restic `rest:` repository URL fresh from each request's headers
  and running restic against it.

Backups can't be deleted through Home Assistant: this location refuses every
delete, including automatic retention. Turn retention off for it, and prune the
repository directly on the restic side when it needs to shrink.

Backups are listed from an index kept in the add-on, so the repository's disk
isn't touched when you browse them. After each successful backup, while the disk
is awake anyway, the integration has the add-on compare the index with the
repository and forget backups you pruned there. The device's "Sync with
repository" button does the same on demand. Between syncs, a pruned backup still
shows up.

When adding the integration (Settings → Devices & Services → Add Integration →
Restic Backup), you'll be asked for:

- **Server URL** — your restic REST server's address (https://github.com/restic/rest-server).
- **Server username** / **Server password** — HTTP Basic Auth for the rest-server
  itself, if it requires one. Entered as separate fields; the integration builds the
  authenticated request internally, you never type a combined URL.
- **Repository password** — the restic repository's own encryption password.
  Unrelated to the server username/password above.

Submitting the form has the add-on actually try to reach the repository with these
credentials (and initialize it, if it doesn't exist yet) before the entry is created.

## Developing the integration

No Home Assistant Core checkout needed — this uses the same devcontainer
pattern as `ludeeus/integration_blueprint`.

1. Open this folder in VS Code and "Reopen in Container" (runs `scripts/setup`,
   which `pip install`s a released `homeassistant` package).
2. Run `scripts/develop` in the integrated terminal. First run writes a throwaway
   `config/configuration.yaml` (gitignored) and boots HA with
   `custom_components/restic_backup` on `PYTHONPATH`, forwarded on port 8123.
   Keep `default_config:` in that file: HA installs the Python packages for the
   integrations it sets up at startup, and the frontend needs those packages even
   if you never use the integrations. They live in `~/.local`, so a container
   rebuild deletes them and the next start downloads them again.
3. To hit breakpoints, add to `config/configuration.yaml`:
   ```yaml
   debugpy:
     start: true
     wait: true
   ```
   then run `scripts/develop` again and use the "Python: Attach to Home Assistant"
   launch config (`.vscode/launch.json`, port 5678) to attach.
4. Add the integration from Settings → Devices & Services, pointing it at a real
   restic REST server. `scripts/develop` also starts the add-on's `server.py` on
   `localhost:8099` and points the integration at it, so no HAOS VM is needed.

The **Server URL** must include the repository path, e.g.
`http://192.168.1.50:16789/myrepo`. With `--private-repos`, the path must match
the server username.

## Developing the add-on

Since it has no options of its own, the add-on itself barely needs "configuring" —
`scripts/develop` runs its `server.py` directly, using the devcontainer's `restic`.
Restart `scripts/develop` to pick up changes.

For the full Supervisor-hosted path (S6 service, `rootfs/`, real add-on lifecycle):

1. Copy (or symlink via the Samba share / SSH & Web Terminal add-on)
   `addon/restic_backup/` into HAOS's local add-ons folder.
2. Settings → Add-ons → Add-on Store → local add-ons → install, start.
3. After each code change: **Rebuild**, then **Restart** — logs are in the
   add-on's Log tab.
4. `curl -X POST -H "X-Rest-Url: http://..." -H "X-Repo-Password: ..." http://<haos-ip>:8099/verify`
   confirms it's up and can reach a real repository before wiring the integration to it.

## End-to-end check

With both sides running and the integration's config entry created: trigger a manual
backup targeting the new Location from HA's Backups page, confirm `restic snapshots`
against the configured rest-server shows a new snapshot tagged with the backup id,
then restore it back through the same UI.
