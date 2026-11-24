"""Runs the wacli executable and parses its JSON envelope."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio

from wacli_mcp.settings import Settings

_STDERR_TAIL_CHARS = 2_000
# Keeps wacli from opening a console window per call when the server runs under a GUI client.
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class WacliError(Exception):
    """wacli failed, timed out, or printed something that is not its JSON envelope."""


@dataclass
class SyncState:
    """Outcome of the most recent pre-read sync attempt."""

    attempted_at: float | None = None
    ok: bool = False
    error: str | None = None


@dataclass
class Wacli:
    """Builds wacli argument lists and runs them without a shell.

    `command` is the argv prefix used to start wacli, for example `["wacli"]`.
    Tests replace it with a fake executable.
    """

    settings: Settings
    command: Sequence[str] = ()
    sync_state: SyncState = field(default_factory=SyncState)
    # Serializes commands that take wacli's store lock (sync and send), so this
    # server never races itself for the lock.
    _write_lock: anyio.Lock = field(default_factory=anyio.Lock)

    def __post_init__(self) -> None:
        if not self.command:
            self.command = (self.settings.wacli_path,)

    def _global_args(self, *, read_only: bool) -> list[str]:
        args = ["--json"]
        if read_only:
            args.append("--read-only")
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
    ) -> Any:
        """Run `wacli <global flags> <args>` and return the envelope's `data`.

        Raises:
            WacliError: on a timeout, a non-JSON response, or `success: false`.
        """
        argv = [*self.command, *self._global_args(read_only=read_only), *args]
        limit = timeout if timeout is not None else self.settings.command_timeout_seconds
        try:
            with anyio.fail_after(limit):
                result = await anyio.run_process(
                    argv,
                    check=False,
                    # The MCP server speaks JSON-RPC on stdin; wacli must never read it.
                    stdin=subprocess.DEVNULL,
                    creationflags=_CREATION_FLAGS,
                )
        except TimeoutError as exc:
            raise WacliError(f"wacli timed out after {limit:g}s") from exc
        except OSError as exc:
            raise WacliError(f"could not start wacli ({self.command[0]!r}): {exc}") from exc

        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            detail = stderr[-_STDERR_TAIL_CHARS:] or stdout[-_STDERR_TAIL_CHARS:] or "no output"
            raise WacliError(f"wacli exited with code {result.returncode}: {detail}") from None
        if not isinstance(envelope, dict) or "success" not in envelope:
            raise WacliError("wacli returned JSON without the expected success/data/error envelope")
        if not envelope["success"]:
            raise WacliError(str(envelope.get("error") or f"wacli exited with code {result.returncode}"))
        return envelope.get("data")

    async def run_write(self, args: Sequence[str], *, timeout: float | None = None) -> Any:
        """Run a command that may change WhatsApp or the local store."""
        async with self._write_lock:
            return await self.run(args, read_only=False, timeout=timeout)

    async def refresh(self) -> SyncState:
        """Pull new messages with a short one-shot sync, at most once per `sync_max_age_seconds`.

        Failures are recorded, not raised: when `wacli sync --follow` owns the store,
        the one-shot sync cannot take the lock, and the store is already current.
        """
        settings = self.settings
        state = self.sync_state
        if not settings.sync_before_read:
            return state
        now = time.monotonic()
        if state.attempted_at is not None and now - state.attempted_at < settings.sync_max_age_seconds:
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
                )
            except WacliError as exc:
                state.ok, state.error = False, str(exc)
            else:
                state.ok, state.error = True, None
            state.attempted_at = time.monotonic()
        return state
