# Restic Backup add-on

Runs the restic part of the **Restic Backup** integration: it receives backups from Home
Assistant and stores them in a restic repository on your
[restic REST server](https://github.com/restic/rest-server).

This add-on has **no configuration**. Start it, then install the integration
(see the project README) and add it under Settings → Devices & Services. The
server URL and passwords are entered there.

## Notes

- The add-on publishes no port. Only Home Assistant can reach it, over the internal add-on network.
- Each Home Assistant backup is one restic snapshot, tagged with the backup's id.
- `/data/index.json` lists the backups, so browsing them doesn't wake the repository's disk.
  Each snapshot also carries its metadata in a tag (`ha-meta=...`), so the index can be rebuilt
  from the repository (the integration's "Sync with repository" button).
- A backup is only stored if all of its bytes arrive. If the connection breaks off, restic is
  stopped before it writes a snapshot.
- restic's cache is kept in `/data/restic-cache`.
