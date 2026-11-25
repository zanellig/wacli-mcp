"""Runs the wacli executable and parses its JSON envelope."""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio
from anyio.abc import ByteReceiveStream, Process

from wacli_mcp.settings import Settings

_STDERR_TAIL_BYTES = 4_000
_DEFAULT_MAX_STDOUT_BYTES = 32 * 1024 * 1024
# Keeps wacli from opening a console window per call when the server runs under a GUI client.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
# wacli's lock error reads "store is locked (another wacli is running?): store locked: ...".
_LOCKED_MARKERS = ("store is locked", "store locked")


class WacliError(Exception):
    """wacli failed, timed out, or printed something that is not its JSON envelope."""


@dataclass
class SyncState:
    """Outcome of the most recent pre-read sync attempt."""

    attempted_at: float | None = None
    ok: bool = False
    locked_by_other: bool = False
    error: str | None = None


def inner_timeout(outer: float) -> float:
    """The `--timeout` passed to wacli: shorter than ours, so wacli gives up first and reports why.

    For sends delegated to `wacli sync --follow`, this also bounds how long the send may wait
    in the daemon's queue, so it cannot go out after this server has reported a timeout.
    """
    return max(1.0, outer - min(10.0, outer * 0.2))


@dataclass
class Wacli:
    """Builds wacli argument lists and runs them without a shell.

    `command` is the argv prefix used to start wacli, for example `["wacli"]`.
    Tests replace it with a fake executable.
    """

    settings: Settings
    command: Sequence[str] = ()
    max_stdout_bytes: int = _DEFAULT_MAX_STDOUT_BYTES
    sync_state: SyncState = field(default_factory=SyncState)
    # Serializes commands that take wacli's store lock (sync and send), so this
    # server never races itself for the lock.
    _write_lock: anyio.Lock = field(default_factory=anyio.Lock)

    def __post_init__(self) -> None:
        if not self.command:
            self.command = (self.settings.wacli_path,)

    def _global_args(self, *, read_only: bool, timeout: float | None, store: str | None) -> list[str]:
        args = ["--json"]
        if read_only:
            args.append("--read-only")
        if timeout is not None:
            args += ["--timeout", f"{timeout:.0f}s"]
        if store:
            # A store folder selects the account completely; wacli refuses `--store` with `--account`.
            args += ["--store", store]
        else:
            if self.settings.account:
                args += ["--account", self.settings.account]
            if self.settings.store:
                args += ["--store", self.settings.store]
        return args

    async def run(
        self,
        args: Sequence[str],
        *,
        read_only: bool = True,
        timeout: float | None = None,
        pass_timeout: bool = True,
        store: str | None = None,
    ) -> Any:
        """Run `wacli <global flags> <args>` and return the envelope's `data`.

        `store` pins the command to one store folder, instead of the account and store settings.

        Raises:
            WacliError: on a timeout, oversized or non-JSON output, or `success: false`.
        """
        limit = timeout if timeout is not None else self.settings.command_timeout_seconds
        global_args = self._global_args(
            read_only=read_only, timeout=inner_timeout(limit) if pass_timeout else None, store=store
        )
        argv = [*self.command, *global_args, *args]
        returncode, stdout_bytes, stderr_bytes = await self._execute(argv, limit)

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            detail = stderr or stdout[-_STDERR_TAIL_BYTES:] or "no output"
            raise WacliError(f"wacli exited with code {returncode}: {detail}") from None
        if not isinstance(envelope, dict) or "success" not in envelope:
            raise WacliError("wacli returned JSON without the expected success/data/error envelope")
        if not envelope["success"]:
            raise WacliError(str(envelope.get("error") or f"wacli exited with code {returncode}"))
        return envelope.get("data")

    async def _execute(self, argv: list[str], limit: float) -> tuple[int | None, bytes, bytes]:
        """Run argv, keeping at most `max_stdout_bytes` of stdout and the tail of stderr."""
        stdout = bytearray()
        stderr_tail = bytearray()
        overflow = False

        async def read_stdout(process: Process, stream: ByteReceiveStream) -> None:
            nonlocal overflow
            async for chunk in stream:
                if len(stdout) + len(chunk) > self.max_stdout_bytes:
                    overflow = True
                    with contextlib.suppress(ProcessLookupError):  # it may have exited already
                        process.kill()
                    return
                stdout.extend(chunk)

        async def read_stderr(stream: ByteReceiveStream) -> None:
            async for chunk in stream:
                stderr_tail.extend(chunk)
                del stderr_tail[:-_STDERR_TAIL_BYTES]

        try:
            with anyio.fail_after(limit):
                # On timeout or cancellation, leaving this block kills and reaps the process.
                async with await anyio.open_process(
                    argv,
                    # The MCP server speaks JSON-RPC on stdin; wacli must never read it.
                    stdin=subprocess.DEVNULL,
                    creationflags=_CREATION_FLAGS,
                ) as process:
                    assert process.stdout is not None and process.stderr is not None
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(read_stdout, process, process.stdout)
                        tg.start_soon(read_stderr, process.stderr)
                    returncode = await process.wait()
        except TimeoutError as exc:
            raise WacliError(f"wacli timed out after {limit:g}s") from exc
        except OSError as exc:
            raise WacliError(f"could not start wacli ({self.command[0]!r}): {exc}") from exc
        if overflow:
            raise WacliError(
                f"wacli printed more than {self.max_stdout_bytes} bytes; narrow the query or lower `limit`"
            )
        return returncode, bytes(stdout), bytes(stderr_tail)

    async def run_write(self, args: Sequence[str], *, timeout: float | None = None) -> Any:
        """Run a command that may change WhatsApp or the local store."""
        async with self._write_lock:
            return await self.run(args, read_only=False, timeout=timeout)

    async def refresh(self) -> SyncState:
        """Pull new messages with a short one-shot sync, at most once per `sync_max_age_seconds`.

        Failures are recorded, not raised. When another wacli process owns the store
        (usually `wacli sync --follow`), the one-shot sync cannot run; that process keeps
        the store current instead.
        """
        settings = self.settings
        state = self.sync_state
        if settings.read_only or not settings.sync_before_read:
            return state
        if state.attempted_at is not None and time.monotonic() - state.attempted_at < settings.sync_max_age_seconds:
            return state
        async with self._write_lock:
            # Another caller may have synced while this one waited for the lock.
            if state.attempted_at is not None and time.monotonic() - state.attempted_at < settings.sync_max_age_seconds:
                return state
            try:
                await self.run(
                    [
                        "sync",
                        "--once",
                        "--idle-exit",
                        f"{settings.sync_idle_exit_seconds:g}s",
                        "--presence-mode",
                        "quiet",
                    ],
                    read_only=False,
                    timeout=settings.sync_timeout_seconds,
                    # `--timeout` does not apply to sync; --idle-exit and our deadline bound it.
                    pass_timeout=False,
                )
            except WacliError as exc:
                message = str(exc)
                state.ok = False
                state.locked_by_other = any(marker in message.lower() for marker in _LOCKED_MARKERS)
                state.error = None if state.locked_by_other else message
            else:
                state.ok, state.locked_by_other, state.error = True, False, None
            state.attempted_at = time.monotonic()
        return state
