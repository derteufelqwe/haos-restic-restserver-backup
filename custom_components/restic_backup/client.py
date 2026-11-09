"""Thin HTTP client for talking to the companion restic-backup add-on.

The add-on itself holds no credentials - every call carries the restic REST
server's connection details and the repository's encryption password, and the
add-on uses them to talk to restic for just that one request.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024

_P = ParamSpec("_P")
_R = TypeVar("_R")


class ResticBackupClientError(Exception):
    """Raised when the add-on, or the restic REST server behind it, can't be reached."""


class ResticBackupNotFoundError(ResticBackupClientError):
    """Raised when the add-on reports a backup id does not exist."""


def _wrap_connection_errors(
    func: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    """Report an unreachable add-on as a ResticBackupClientError."""

    @wraps(func)
    async def _wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await func(*args, **kwargs)
        except (ClientError, TimeoutError) as err:
            raise ResticBackupClientError(f"cannot reach the add-on: {err}") from err

    return _wrapped


class ResticBackupClient:
    """Talks to the restic-backup add-on's HTTP API."""

    def __init__(
        self,
        session: ClientSession,
        addon_url: str,
        rest_url: str,
        rest_username: str,
        rest_password: str,
        repo_password: str,
    ) -> None:
        self._session = session
        self._addon_url = addon_url.rstrip("/")
        self._headers = {
            "X-Rest-Url": rest_url,
            "X-Rest-Username": rest_username,
            "X-Rest-Password": rest_password,
            "X-Repo-Password": repo_password,
        }

    @_wrap_connection_errors
    async def verify(self) -> None:
        """Check that the add-on can reach the restic REST server with these credentials."""
        async with self._session.post(
            f"{self._addon_url}/verify",
            headers=self._headers,
            # The repository's disk may have to spin up first.
            timeout=ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                raise ResticBackupClientError(
                    f"could not verify restic repository: {await resp.text()}"
                )

    @_wrap_connection_errors
    async def sync(self) -> dict[str, list[str]]:
        """Make the add-on's index match the repository.

        Backups that no longer exist in the repository are forgotten, backups that
        are missing from the index are added back. Reads the repository, so this
        wakes its disk. Returns the "removed" and "added" backup ids.
        """
        async with self._session.post(
            f"{self._addon_url}/sync",
            headers=self._headers,
            # The repository's disk may have to spin up first.
            timeout=ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                raise ResticBackupClientError(
                    f"could not sync with the restic repository: {await resp.text()}"
                )
            result = await resp.json()
        if result["removed"]:
            _LOGGER.info(
                "Forgot backups that were removed from restic: %s", result["removed"]
            )
        if result["added"]:
            _LOGGER.info("Found backups in restic: %s", result["added"])
        return result

    @_wrap_connection_errors
    async def list_backups(self) -> list[dict[str, Any]]:
        async with self._session.get(
            f"{self._addon_url}/backups", headers=self._headers
        ) as resp:
            self._raise_for_status(resp.status, "")
            data = await resp.json()
            return data["backups"]

    @_wrap_connection_errors
    async def get_backup(self, backup_id: str) -> dict[str, Any]:
        async with self._session.get(
            f"{self._addon_url}/backups/{backup_id}", headers=self._headers
        ) as resp:
            self._raise_for_status(resp.status, backup_id)
            return await resp.json()

    @_wrap_connection_errors
    async def download_backup(self, backup_id: str) -> AsyncIterator[bytes]:
        resp = await self._session.get(
            f"{self._addon_url}/backups/{backup_id}/download",
            headers=self._headers,
            # The default total timeout of 5 minutes would cut off large downloads.
            # The first bytes can take a while: the repository's disk may spin up.
            timeout=ClientTimeout(total=None, sock_connect=30, sock_read=300),
        )
        if resp.status >= 400 and resp.status != 404:
            detail = await resp.text()
            resp.release()
            raise ResticBackupClientError(f"restic download failed: {detail}")
        self._raise_for_status(resp.status, backup_id)

        async def _iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    yield chunk
            except (ClientError, TimeoutError) as err:
                raise ResticBackupClientError(f"download was interrupted: {err}") from err
            finally:
                resp.release()

        return _iterator()

    @_wrap_connection_errors
    async def upload_backup(
        self,
        backup_id: str,
        metadata: dict[str, Any],
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        on_progress: Callable[[int], None],
    ) -> None:
        stream = await open_stream()
        bytes_sent = 0

        async def _body() -> AsyncIterator[bytes]:
            nonlocal bytes_sent
            async for chunk in stream:
                bytes_sent += len(chunk)
                on_progress(bytes_sent)
                yield chunk

        headers = dict(self._headers)
        headers["X-Backup-Metadata"] = _encode_metadata(metadata)
        async with self._session.post(
            f"{self._addon_url}/backups/{backup_id}/upload",
            headers=headers,
            data=_body(),
            timeout=ClientTimeout(total=None),
        ) as resp:
            self._raise_for_status(resp.status, backup_id)

    @staticmethod
    def _raise_for_status(status: int, backup_id: str) -> None:
        if status == 404:
            raise ResticBackupNotFoundError(backup_id)
        if status >= 400:
            raise ResticBackupClientError(f"add-on returned {status} for {backup_id!r}")


def _encode_metadata(metadata: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(metadata).encode()).decode()
