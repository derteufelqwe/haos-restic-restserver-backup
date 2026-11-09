"""Local metadata index mapping backup ids to restic snapshot ids.

restic itself has no structured metadata store beyond tags, so this file is
what the add-on lists backups from, without touching the repository's disk.
Every snapshot also carries its backup's metadata in a tag, so a lost index
can be rebuilt from the repository (see the add-on's /sync).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("restic_backup")


class IndexStore:
    """A tiny JSON-file backed key/value store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            self._data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            # Start empty (the next sync rebuilds it) instead of failing to start.
            broken = self._path.with_name(self._path.name + ".corrupt")
            _LOGGER.error("Could not read %s (%s); moving it to %s", self._path, err, broken)
            try:
                os.replace(self._path, broken)
            except OSError:
                pass

    def _save(self) -> None:
        # Write a new file and swap it in, so a crash never leaves a half-written index.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._data))
        os.replace(tmp, self._path)

    def put(self, backup_id: str, snapshot_id: str, metadata: dict[str, Any]) -> None:
        self._data[backup_id] = {"snapshot_id": snapshot_id, "metadata": metadata}
        self._save()

    def add_missing(self, entries: dict[str, dict[str, Any]]) -> list[str]:
        """Add entries (`snapshot_id` + `metadata`) for ids that aren't indexed yet."""
        added = [backup_id for backup_id in entries if backup_id not in self._data]
        for backup_id in added:
            entry = entries[backup_id]
            self._data[backup_id] = {
                "snapshot_id": entry["snapshot_id"],
                "metadata": entry["metadata"],
            }
        if added:
            self._save()
        return added

    def get(self, backup_id: str) -> dict[str, Any] | None:
        return self._data.get(backup_id)

    def ids(self) -> set[str]:
        return set(self._data)

    def remove(self, backup_ids: Iterable[str]) -> None:
        """Forget entries whose snapshots no longer exist in the repository."""
        removed = [self._data.pop(backup_id, None) for backup_id in backup_ids]
        if any(entry is not None for entry in removed):
            self._save()

    def list_all(self) -> list[dict[str, Any]]:
        return [entry["metadata"] for entry in self._data.values()]
