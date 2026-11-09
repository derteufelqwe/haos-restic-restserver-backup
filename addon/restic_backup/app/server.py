"""HTTP API bridging the restic_backup Core integration to restic.

This add-on holds no restic credentials of its own. Every request that talks to
restic carries the restic REST server's connection details (X-Rest-Url/-Username/
-Password) and the repository's own encryption password (X-Repo-Password) in its
headers, and a `Restic` instance is built fresh for that one request.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from index_store import IndexStore
from restic import Restic, ResticError

logging.basicConfig(level=logging.INFO)
_LOGGER = logging.getLogger("restic_backup")

DATA_DIR = Path(os.environ.get("RESTIC_BACKUP_DATA_DIR", "/data"))
CACHE_DIR = DATA_DIR / "restic-cache"


def _snapshot_filename(backup_id: str) -> str:
    return f"{backup_id}.tar"


def with_restic(handler):
    async def _wrapped(request: web.Request):
        headers = request.headers
        try:
            restic = Restic.for_rest_server(
                rest_url=headers["X-Rest-Url"],
                rest_username=headers.get("X-Rest-Username", ""),
                rest_password=headers.get("X-Rest-Password", ""),
                repo_password=headers["X-Repo-Password"],
                cache_dir=CACHE_DIR,
            )
        except KeyError as err:
            raise web.HTTPBadRequest(text=f"missing header: {err}") from None
        return await handler(request, restic)

    return _wrapped


def _parse_metadata(request: web.Request, backup_id: str) -> dict[str, Any]:
    """Decode the X-Backup-Metadata header. It must carry the backup's byte size."""
    try:
        metadata = json.loads(
            base64.b64decode(request.headers.get("X-Backup-Metadata", ""), validate=True)
        )
        size = metadata["size"]
        valid = (
            isinstance(size, int)
            and not isinstance(size, bool)
            and size >= 0
            and metadata.get("backup_id") == backup_id
        )
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise web.HTTPBadRequest(
            text="X-Backup-Metadata must be base64 JSON with this backup_id and an integer size"
        )
    return metadata


async def health(request: web.Request) -> web.Response:
    """Answer without touching restic or the repository, for the Supervisor watchdog."""
    return web.json_response({"status": "ok"})


@with_restic
async def verify(request: web.Request, restic: Restic) -> web.Response:
    try:
        await restic.init_repository()
    except ResticError as err:
        raise web.HTTPBadGateway(text=str(err)) from err
    return web.json_response({"status": "ok"})


async def list_backups(request: web.Request) -> web.Response:
    store: IndexStore = request.app["store"]
    return web.json_response({"backups": store.list_all()})


async def get_backup(request: web.Request) -> web.Response:
    store: IndexStore = request.app["store"]
    entry = store.get(request.match_info["backup_id"])
    if entry is None:
        raise web.HTTPNotFound()
    return web.json_response(entry["metadata"])


@with_restic
async def upload_backup(request: web.Request, restic: Restic) -> web.Response:
    store: IndexStore = request.app["store"]
    backup_id = request.match_info["backup_id"]
    metadata = _parse_metadata(request, backup_id)

    async def _body():
        async for chunk in request.content.iter_any():
            yield chunk

    try:
        snapshot_id = await restic.backup_stream(
            tag=backup_id,
            metadata=metadata,
            filename=_snapshot_filename(backup_id),
            body=_body(),
            expected_size=metadata["size"],
        )
    except ResticError as err:
        _LOGGER.error("backup failed: %s", err)
        raise web.HTTPInternalServerError(text=str(err)) from err

    store.put(backup_id, snapshot_id, metadata)
    return web.json_response({"snapshot_id": snapshot_id})


@with_restic
async def download_backup(request: web.Request, restic: Restic) -> web.StreamResponse:
    store: IndexStore = request.app["store"]
    backup_id = request.match_info["backup_id"]

    entry = store.get(backup_id)
    if entry is None:
        raise web.HTTPNotFound()

    stream = await restic.dump_stream(entry["snapshot_id"], _snapshot_filename(backup_id))
    # Closing the stream stops restic, also when the client goes away midway.
    async with contextlib.aclosing(stream):
        # Read the first chunk before answering, so a failure that happens right away
        # (e.g. the snapshot was pruned) is reported as an error status. Once the
        # response has started, the status can't change any more.
        try:
            first_chunk = await anext(stream, b"")
        except ResticError as err:
            _LOGGER.error("download failed: %s", err)
            raise web.HTTPInternalServerError(text=str(err)) from err

        response = web.StreamResponse()
        await response.prepare(request)
        try:
            await response.write(first_chunk)
            async for chunk in stream:
                await response.write(chunk)
        except ResticError as err:
            # Too late for a status code: cut the connection so the client sees a
            # broken download instead of a complete-looking, truncated one.
            _LOGGER.error("download failed midway: %s", err)
            request.transport.close()
            raise
        await response.write_eof()
        return response


@with_restic
async def sync_index(request: web.Request, restic: Restic) -> web.Response:
    """Make the index match the repository.

    Backups that were removed from the repository are forgotten. Backups that are in
    the repository but not in the index (e.g. after losing /data) are added back from
    the metadata stored in their snapshots.
    """
    store: IndexStore = request.app["store"]
    # Only judge entries that existed before restic was asked, so a backup that
    # finishes uploading in the meantime is never removed.
    known = store.ids()
    try:
        found = await restic.backups()
    except ResticError as err:
        _LOGGER.error("sync failed: %s", err)
        raise web.HTTPBadGateway(text=str(err)) from err

    removed = sorted(known - found.keys())
    store.remove(removed)
    added = sorted(
        store.add_missing(
            {
                backup_id: entry
                for backup_id, entry in found.items()
                if entry["metadata"] is not None
            }
        )
    )
    return web.json_response({"removed": removed, "added": added})


def create_app() -> web.Application:
    app = web.Application()
    app["store"] = IndexStore(DATA_DIR / "index.json")

    app.add_routes(
        [
            web.get("/health", health),
            web.post("/verify", verify),
            web.post("/sync", sync_index),
            web.get("/backups", list_backups),
            web.get("/backups/{backup_id}", get_backup),
            web.post("/backups/{backup_id}/upload", upload_backup),
            web.get("/backups/{backup_id}/download", download_backup),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(create_app(), port=8099)
