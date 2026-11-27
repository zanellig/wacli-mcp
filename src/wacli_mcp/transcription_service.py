"""One transcription process per user, shared by every wacli-mcp session.

Each agent session (a Claude Code or Codex window, a T3 thread) runs its own wacli-mcp process. If each
loaded Whisper itself, every session that transcribed would keep its own ~2 GB copy in GPU memory.
Instead, the first session that needs a transcript starts this service in the background, and every
session sends it file paths:

- The connection is a named pipe on Windows and a Unix socket elsewhere, never a network port.
- Both ends prove they hold a random key from a file only this user can read: the HMAC handshake of
  `multiprocessing.connection`. Messages are JSON, never pickles.
- The service keeps one model loaded, transcribes one file at a time, transcribes a file that several
  sessions ask for at once only once, and exits after a stretch with no requests, freeing the GPU.

Run as `python -m wacli_mcp.transcription_service --dir <folder> --idle-exit <seconds>`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Protocol

from wacli_mcp.settings import Settings
from wacli_mcp.transcription import Transcriber, Transcript, TranscriptionError, WhisperTranscriber

PROTOCOL = 1
_MAX_MESSAGE_BYTES = 1_000_000
_MAX_LOG_BYTES = 1_000_000
# Errors that mean no service is there to answer: no pipe, no socket file, a stale socket file, or a
# service that exited (idle) while the connection was being set up, which ends the handshake early.
_NOT_RUNNING = (FileNotFoundError, ConnectionError, EOFError)
# How long a new service waits for one that is exiting to let go of the lock or the pipe.
_HANDOVER_SECONDS = 2.0


class ServiceUnavailableError(Exception):
    """The shared service could not be reached or started."""


class Channel(Protocol):
    """The part of a `multiprocessing` connection (socket or pipe) that the service uses."""

    def send_bytes(self, buf: bytes) -> None: ...

    def recv_bytes(self, maxlength: int | None = None) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ServiceAddress:
    """Where the service for one folder listens, and the files it keeps there."""

    folder: Path
    address: str
    family: str

    @classmethod
    def for_folder(cls, folder: Path) -> ServiceAddress:
        folder = folder.resolve()
        digest = hashlib.sha256(str(folder).encode()).hexdigest()[:16]
        if sys.platform == "win32":
            return cls(folder, rf"\\.\pipe\wacli-mcp-transcriber-{digest}", "AF_PIPE")
        socket = folder / "transcriber.sock"
        # Unix socket paths are limited to about 104 bytes. A shorter path elsewhere is fine: the
        # socket file is made readable by this user only, and both ends authenticate.
        if len(str(socket).encode()) > 100:
            socket = Path(tempfile.gettempdir()) / f"wacli-mcp-{digest}.sock"
        return cls(folder, str(socket), "AF_UNIX")

    @property
    def key_file(self) -> Path:
        return self.folder / "transcriber.key"

    @property
    def log_file(self) -> Path:
        return self.folder / "transcriber.log"

    def key(self) -> bytes:
        """The shared secret, created on first use in a file only this user can read."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for _ in range(100):
            try:
                handle = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                text = self.key_file.read_text(encoding="ascii").strip()
                if text:
                    return bytes.fromhex(text)
                time.sleep(0.02)  # another process has just created it and is writing it
                continue
            with os.fdopen(handle, "w", encoding="ascii") as file:
                file.write(secrets.token_hex(32))
        raise ServiceUnavailableError(f"could not read {self.key_file}")

    def connect(self) -> Channel:
        return Client(self.address, family=self.family, authkey=self.key())


# The service side.


class Backend(Protocol):
    def transcribe_in(self, path: Path, language: str | None) -> Transcript: ...

    def status(self) -> dict[str, Any]: ...


BackendFactory = Callable[[str, str], Backend]


def whisper_backend(model: str, device: str) -> Backend:
    options: dict[str, Any] = {"transcribe_model": model, "transcribe_device": device}
    return WhisperTranscriber(Settings(**options))


@dataclass
class _Pending:
    done: threading.Event = field(default_factory=threading.Event)
    result: Transcript | None = None
    error: str | None = None


class _Service:
    def __init__(self, idle_seconds: float, backend_factory: BackendFactory) -> None:
        self.idle_seconds = idle_seconds
        self.backend_factory = backend_factory
        self.lock = threading.Lock()
        self.connections = 0
        self.last_activity = time.monotonic()
        self.pending: dict[tuple[str, str, str, str | None], _Pending] = {}
        self.backend_lock = threading.Lock()
        self.backend_key: tuple[str, str] | None = None
        self.backend: Backend | None = None

    def watch_idle(self) -> None:
        while True:
            time.sleep(min(5.0, max(0.2, self.idle_seconds / 4)))
            with self.lock:
                idle = self.connections == 0 and time.monotonic() - self.last_activity > self.idle_seconds
            if idle:
                # Exiting is how the model leaves GPU memory.
                os._exit(0)

    def handle(self, connection: Channel) -> None:
        try:
            with contextlib.closing(connection):
                while True:
                    try:
                        request = json.loads(connection.recv_bytes(_MAX_MESSAGE_BYTES))
                    except (EOFError, OSError, ValueError):
                        return
                    response = self.respond(request)
                    connection.send_bytes(json.dumps(response).encode())
                    if isinstance(request, dict) and request.get("op") == "stop":
                        os._exit(0)
        except OSError:
            return
        finally:
            with self.lock:
                self.connections -= 1

    def respond(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
            return {
                "ok": False,
                "protocol": PROTOCOL,
                "error": f"this transcription service speaks protocol {PROTOCOL}",
            }
        op = request.get("op")
        if op == "status":
            return {"ok": True, "status": self.status()}
        if op == "stop":
            return {"ok": True}
        if op != "transcribe":
            return {"ok": False, "error": f"unknown request {op!r}"}
        path, model, device, language = (request.get(name) for name in ("path", "model", "device", "language"))
        if not (isinstance(path, str) and isinstance(model, str) and isinstance(device, str)):
            return {"ok": False, "error": "a transcription request needs `path`, `model` and `device`"}
        if language is not None and not isinstance(language, str):
            return {"ok": False, "error": "`language` must be a string or null"}
        try:
            transcript = self.transcribe(Path(path), model, device, language)
        except TranscriptionError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "transcript": asdict(transcript)}

    def transcribe(self, path: Path, model: str, device: str, language: str | None) -> Transcript:
        """Transcribe once per file at a time, even when several sessions ask for it together."""
        key = (str(path), model, device, language)
        with self.lock:
            # Only transcriptions count as activity: status checks must not keep the model loaded.
            self.last_activity = time.monotonic()
            pending = self.pending.get(key)
            owner = pending is None
            if pending is None:
                pending = self.pending[key] = _Pending()
        if owner:
            try:
                pending.result = self.backend_for(model, device).transcribe_in(path, language)
            # Decoder and model errors have many types; each goes back to the session that asked.
            except Exception as exc:  # noqa: BLE001
                pending.error = str(exc)
            finally:
                with self.lock:
                    del self.pending[key]
                    self.last_activity = time.monotonic()
                pending.done.set()
        else:
            pending.done.wait()
        if pending.result is None:
            raise TranscriptionError(pending.error or "transcription stopped without a result")
        return pending.result

    def backend_for(self, model: str, device: str) -> Backend:
        with self.backend_lock:
            if self.backend is None or self.backend_key != (model, device):
                self.backend = None  # let the previous model go before loading another
                self.backend = self.backend_factory(model, device)
                self.backend_key = (model, device)
            return self.backend

    def status(self) -> dict[str, Any]:
        with self.lock:
            busy = bool(self.pending)
        backend = self.backend
        return {
            "pid": os.getpid(),
            "busy": busy,
            "idle_exit_seconds": self.idle_seconds,
            "model": self.backend_key[0] if self.backend_key else None,
            "backend": backend.status() if backend is not None else None,
        }


@contextlib.contextmanager
def _first_instance(where: ServiceAddress) -> Iterator[bool]:
    """Whether this process may run the service for `where`. Held for the life of the process."""
    if sys.platform == "win32":
        # A named pipe is created with FILE_FLAG_FIRST_PIPE_INSTANCE, so a second service fails to listen.
        yield True
        return
    import fcntl

    with open(where.folder / "transcriber.lock", "a+") as handle:
        deadline = time.monotonic() + _HANDOVER_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                # Held by a running service, or by one still exiting after its idle timeout.
                if time.monotonic() > deadline:
                    yield False
                    return
                time.sleep(0.1)
        yield True


def serve(folder: Path, idle_seconds: float, backend_factory: BackendFactory = whisper_backend) -> int:
    """Run the service until it has been idle for `idle_seconds`. Returns at once if one is already running."""
    where = ServiceAddress.for_folder(folder)
    key = where.key()
    with _first_instance(where) as first:
        if not first:
            return 0
        if where.family == "AF_UNIX":
            # Safe while holding the lock: no other service is listening on it.
            Path(where.address).unlink(missing_ok=True)
        deadline = time.monotonic() + _HANDOVER_SECONDS
        while True:
            try:
                listener = Listener(where.address, family=where.family, authkey=key)
                break
            except OSError:
                # Another service owns the pipe; wait briefly in case it is exiting.
                if time.monotonic() > deadline:
                    return 0
                time.sleep(0.1)
        if where.family == "AF_UNIX":
            os.chmod(where.address, 0o600)
        service = _Service(idle_seconds, backend_factory)
        threading.Thread(target=service.watch_idle, name="idle-exit", daemon=True).start()
        while True:
            try:
                connection = listener.accept()
            except (AuthenticationError, EOFError, OSError):
                continue  # a client without the key, or one that went away mid-handshake
            with service.lock:
                service.connections += 1
            threading.Thread(target=service.handle, args=(connection,), daemon=True).start()


# The session side.


class SharedTranscriber:
    """A `Transcriber` that sends work to the shared service, starting it when needed.

    If the service can't be reached or started, this session transcribes in its own process instead,
    and `status()` says why.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        service_command: Sequence[str] | None = None,
        fallback: Transcriber | None = None,
        start_timeout: float = 30.0,
    ) -> None:
        self._settings = settings
        self._local = WhisperTranscriber(settings)
        self._fallback = fallback or self._local
        folder = settings.media_root / ".transcriber"
        self._where = ServiceAddress.for_folder(folder)
        self._command = list(service_command or [sys.executable, "-m", "wacli_mcp.transcription_service"])
        self._start_timeout = start_timeout
        self._spawn_lock = threading.Lock()
        self._started: subprocess.Popen[Any] | None = None
        self._service_error: str | None = None

    @property
    def cache_tag(self) -> str:
        return self._local.cache_tag

    def unavailable_reason(self) -> str | None:
        # The service runs with this session's Python, so it can transcribe only if this one could.
        return self._local.unavailable_reason()

    def status(self) -> dict[str, Any]:
        info = self._local.status()
        info.pop("device", None)
        info["mode"] = "shared service"
        try:
            response = self._request({"op": "status"}, start=False)
        except ServiceUnavailableError:
            response = None
        if response and response.get("ok"):
            info["service"] = response["status"]
        else:
            info["service"] = "not running; it starts with the next transcription"
        if self._service_error:
            info["service_error"] = self._service_error
            info["fallback"] = "this session transcribes in its own process until the service works"
        return info

    def transcribe(self, path: Path) -> Transcript:
        request = {
            "op": "transcribe",
            "path": str(path),
            "model": self._settings.transcribe_model,
            "device": self._settings.transcribe_device,
            "language": self._settings.transcribe_language,
        }
        try:
            response = self._request(request, start=True)
        except ServiceUnavailableError as exc:
            self._service_error = str(exc)
            return self._fallback.transcribe(path)
        self._service_error = None
        if response is None or not response.get("ok"):
            raise TranscriptionError(str((response or {}).get("error") or "the transcription service failed"))
        return Transcript(**response["transcript"])

    def stop_service(self) -> None:
        """Ask a running service to exit now. Does nothing when none is running."""
        with contextlib.suppress(ServiceUnavailableError):
            self._request({"op": "stop"}, start=False)

    def _request(self, message: dict[str, Any], *, start: bool) -> dict[str, Any] | None:
        """Send one request. Returns None when no service runs and `start` is false."""
        payload = json.dumps({"protocol": PROTOCOL, **message}).encode()
        failure: Exception | None = None
        # A service can exit between connecting and answering (idle exit, a crash): try once more.
        for _ in range(2):
            try:
                connection = self._connect(start=start)
            except _NOT_RUNNING:
                return None
            try:
                with contextlib.closing(connection):
                    connection.send_bytes(payload)
                    response = json.loads(connection.recv_bytes(_MAX_MESSAGE_BYTES))
            except (EOFError, OSError, ValueError) as exc:
                failure = exc
                continue
            if not isinstance(response, dict):
                return None
            if response.get("protocol", PROTOCOL) != PROTOCOL:
                # A service started by another wacli-mcp version; it exits once idle.
                raise ServiceUnavailableError(str(response.get("error")))
            return response
        raise ServiceUnavailableError(f"the transcription service stopped answering: {failure}")

    def _open(self) -> Channel:
        try:
            return self._where.connect()
        except AuthenticationError as exc:
            raise ServiceUnavailableError(f"something else is listening at {self._where.address}") from exc

    def _connect(self, *, start: bool) -> Channel:
        if not start and not self._where.key_file.exists():
            raise FileNotFoundError("no service has run yet")
        try:
            return self._open()
        except _NOT_RUNNING:
            if not start:
                raise
        with self._spawn_lock:
            # Another thread may have started it while this one waited for the lock.
            with contextlib.suppress(*_NOT_RUNNING):
                return self._open()
            self._spawn()
            deadline = time.monotonic() + self._start_timeout
            while time.monotonic() < deadline:
                time.sleep(0.1)
                with contextlib.suppress(*_NOT_RUNNING):
                    return self._open()
        raise ServiceUnavailableError(
            f"the transcription service did not start within {self._start_timeout:g} s; see {self._where.log_file}"
        )

    def _spawn(self) -> None:
        folder = self._where.folder
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._where.key()
        if self._started is not None:
            self._started.poll()  # collect an earlier service that has exited
        log = self._where.log_file
        with contextlib.suppress(OSError):
            if log.stat().st_size > _MAX_LOG_BYTES:
                log.unlink()
        command = [
            *self._command,
            "--dir",
            str(folder),
            "--idle-exit",
            f"{self._settings.transcribe_idle_minutes * 60:g}",
        ]
        # The service must never write to this process's stdout, which carries the MCP protocol.
        with open(log, "ab") as output:
            options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": output, "stderr": subprocess.STDOUT}
            if sys.platform == "win32":
                # Detached, and outside the job object of the session that started it, so it keeps
                # serving other sessions when this one closes. Some job objects forbid breaking away.
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                try:
                    self._started = subprocess.Popen(
                        command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options
                    )
                except OSError:
                    self._started = subprocess.Popen(command, creationflags=flags, **options)
            else:
                self._started = subprocess.Popen(command, start_new_session=True, **options)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shared Whisper transcription service for wacli-mcp.")
    parser.add_argument("--dir", required=True, type=Path, help="Folder for the key, lock and log files.")
    parser.add_argument("--idle-exit", type=float, default=600, help="Exit after this many idle seconds.")
    args = parser.parse_args(argv)
    return serve(args.dir, args.idle_exit)


if __name__ == "__main__":
    sys.exit(main())
