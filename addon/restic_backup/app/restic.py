"""Subprocess wrapper around the restic binary."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

# Every snapshot also carries the backup's metadata in a tag with this prefix, so
# the add-on's index can be rebuilt from the repository if it is ever lost.
META_TAG_PREFIX = "ha-meta="

# restic's exit code for "there is no repository at this location" (restic >= 0.17).
EXIT_NO_REPOSITORY = 10

# Upper bound for the short restic calls. The repository's disk may have to spin up,
# and restic retries unreachable servers for a long time. Shorter than the 120 s the
# integration waits, so it is this add-on that reports the timeout.
COMMAND_TIMEOUT = 100


class ResticError(Exception):
    """Raised when a restic invocation fails."""


def build_rest_repository(rest_url: str, rest_username: str, rest_password: str) -> str:
    """Build a restic `rest:` repository string from a rest-server's connection details.

    rest_username/rest_password are kept separate from rest_url (rather than requiring
    them embedded in it) and are only added to the URL here if the rest-server actually
    requires HTTP Basic Auth.
    """
    parts = urlsplit(rest_url)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal: urlsplit() strips the brackets
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc += f":{parts.port}"
    if rest_username or rest_password:
        netloc = f"{quote(rest_username, safe='')}:{quote(rest_password, safe='')}@{netloc}"
    rebuilt = urlunsplit((parts.scheme or "http", netloc, parts.path, "", ""))
    return f"rest:{rebuilt}"


def encode_metadata(metadata: dict[str, Any]) -> str:
    """Pack backup metadata into a string that is safe to use as a restic tag."""
    packed = zlib.compress(json.dumps(metadata, separators=(",", ":")).encode())
    return base64.urlsafe_b64encode(packed).decode()


def decode_metadata(token: str) -> dict[str, Any] | None:
    """Reverse of encode_metadata. Returns None for anything that isn't valid."""
    try:
        metadata = json.loads(zlib.decompress(base64.urlsafe_b64decode(token)))
    except (ValueError, zlib.error):
        return None
    return metadata if isinstance(metadata, dict) else None


def _error_text(stderr: bytes) -> str:
    """restic's error output as plain text. With --json it is a JSON object."""
    text = stderr.decode(errors="replace").strip()
    for line in reversed(text.splitlines()):
        try:
            message = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        if isinstance(message, str):
            return message
    return text


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Stop a process that is still running and wait for it. No-op once it exited."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def _abort_backup(proc: asyncio.subprocess.Process) -> None:
    """Abort a running `restic backup` without letting it write a snapshot.

    SIGTERM makes restic cancel the backup and remove its repository lock. A kill
    would leave a stale lock behind, which blocks pruning on the server until
    someone runs `restic unlock`. So that is only the fallback.
    """
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except asyncio.TimeoutError:
            await _kill(proc)


class Restic:
    """Runs restic against a single restic repository for the duration of one request."""

    def __init__(
        self, repository: str, password: str, cache_dir: Path | str | None = None
    ) -> None:
        self._env = {
            "RESTIC_REPOSITORY": repository,
            "RESTIC_PASSWORD": password,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        if cache_dir is not None:
            # There is no HOME, so restic would otherwise run without a cache.
            self._env["RESTIC_CACHE_DIR"] = str(cache_dir)

    @classmethod
    def for_rest_server(
        cls,
        rest_url: str,
        rest_username: str,
        rest_password: str,
        repo_password: str,
        cache_dir: Path | str | None = None,
    ) -> "Restic":
        return cls(
            build_rest_repository(rest_url, rest_username, rest_password),
            repo_password,
            cache_dir,
        )

    async def _run(
        self, *args: str, timeout: float = COMMAND_TIMEOUT
    ) -> tuple[int, bytes, bytes]:
        """Run a short restic command. Returns (exit code, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "restic",
            *args,
            env=self._env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await _kill(proc)
            raise ResticError(f"restic {args[0]} timed out after {timeout:.0f} s") from None
        except BaseException:
            await _kill(proc)
            raise
        assert proc.returncode is not None
        return proc.returncode, stdout, stderr

    async def init_repository(self) -> None:
        """Check that the repository can be opened, and create it if there is none.

        Only a repository that does not exist is created. Every other failure (wrong
        repository password, wrong server credentials, server unreachable) is
        reported as it is, so a typo can never silently create a second repository.
        """
        code, _, stderr = await self._run("snapshots", "--no-lock")
        if code == 0:
            return
        if code != EXIT_NO_REPOSITORY:
            raise ResticError(f"restic snapshots failed: {_error_text(stderr)}")

        code, _, stderr = await self._run("init")
        if code != 0:
            raise ResticError(f"restic init failed: {_error_text(stderr)}")

    async def backups(self) -> dict[str, dict[str, Any]]:
        """Return the backups that exist in the repository, by backup id.

        Each uploaded backup is tagged with its backup id, so the tags are the set of
        backups that still exist. Every value holds the newest `snapshot_id`, its `time`
        and the backup's `metadata` (None for snapshots without a metadata tag).
        """
        code, stdout, stderr = await self._run("snapshots", "--json", "--no-lock")
        if code != 0:
            raise ResticError(f"restic snapshots failed: {_error_text(stderr)}")
        try:
            items = json.loads(stdout) or []
        except ValueError as err:
            raise ResticError(f"restic snapshots returned invalid JSON: {err}") from err

        # A flat list of snapshots, or (with grouping) a list of groups holding them.
        snapshots = [
            snapshot
            for item in items
            for snapshot in (item["snapshots"] if "snapshots" in item else [item])
        ]

        found: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            tags = snapshot.get("tags") or []
            metadata = None
            for tag in tags:
                if tag.startswith(META_TAG_PREFIX):
                    metadata = decode_metadata(tag[len(META_TAG_PREFIX) :])
            for tag in tags:
                if tag.startswith(META_TAG_PREFIX):
                    continue
                entry = {
                    "snapshot_id": snapshot["id"],
                    "time": snapshot.get("time", ""),
                    "metadata": (
                        metadata if metadata and metadata.get("backup_id") == tag else None
                    ),
                }
                # The newest snapshot wins if a backup was uploaded more than once.
                if tag not in found or entry["time"] > found[tag]["time"]:
                    found[tag] = entry
        return found

    async def backup_stream(
        self,
        tag: str,
        metadata: dict[str, Any],
        filename: str,
        body: AsyncIterator[bytes],
        expected_size: int,
    ) -> str:
        """Back up a byte stream as a single file, tagged with `tag`. Returns snapshot id.

        The stream must be exactly `expected_size` bytes long. If the connection
        broke off and the stream ended early, restic is killed before it can write
        the snapshot, so a truncated backup never ends up in the repository.
        """
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "backup",
            "--stdin",
            "--stdin-filename",
            filename,
            "--tag",
            tag,
            "--tag",
            META_TAG_PREFIX + encode_metadata(metadata),
            "--json",
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _feed() -> None:
            assert proc.stdin is not None
            received = 0
            try:
                async for chunk in body:
                    received += len(chunk)
                    if received > expected_size:
                        raise ResticError(
                            f"upload is larger than the announced {expected_size} bytes"
                        )
                    try:
                        proc.stdin.write(chunk)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        # restic exited early; its exit code and stderr are reported
                        # below. Only the write to restic is covered here: a broken
                        # connection to the client (which is also a
                        # ConnectionResetError) must still fail the upload.
                        return
                if received != expected_size:
                    raise ResticError(
                        f"upload was incomplete: got {received} of {expected_size} bytes"
                    )
            except BaseException:
                # Not a clean end of the stream: stop restic before it sees an EOF
                # (stdin is only closed afterwards) and writes a snapshot of
                # incomplete data.
                await _abort_backup(proc)
                raise
            finally:
                proc.stdin.close()

        assert proc.stdout is not None and proc.stderr is not None
        try:
            # Not communicate(): it closes stdin itself, before _feed has written anything.
            _, stdout, stderr = await asyncio.gather(
                _feed(), proc.stdout.read(), proc.stderr.read()
            )
            await proc.wait()
        finally:
            await _kill(proc)

        if proc.returncode != 0:
            raise ResticError(f"restic backup failed: {_error_text(stderr)}")

        return _parse_snapshot_id(stdout.decode())

    async def dump_stream(self, snapshot_id: str, filename: str) -> AsyncIterator[bytes]:
        """Stream a previously backed-up file's content back out.

        Close the returned iterator (aclose) when done early: that stops restic.
        """
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "dump",
            "--no-lock",
            snapshot_id,
            f"/{filename}",
            env=self._env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        # Drained all the time, or a restic that logs a lot would block on a full pipe.
        stderr_task = asyncio.create_task(proc.stderr.read())

        async def _iterator() -> AsyncIterator[bytes]:
            try:
                while chunk := await proc.stdout.read(1024 * 1024):
                    yield chunk
                await proc.wait()
                if proc.returncode != 0:
                    stderr = await stderr_task
                    raise ResticError(f"restic dump failed: {_error_text(stderr)}")
            finally:
                # The client may have gone away: don't leave restic running.
                await _kill(proc)
                stderr_task.cancel()

        return _iterator()


def _parse_snapshot_id(json_lines: str) -> str:
    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("message_type") == "summary":
            return event["snapshot_id"]
    raise ResticError("restic backup did not report a snapshot id")
