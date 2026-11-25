"""Downloads message attachments with wacli and transcribes audio, caching both on disk."""

from __future__ import annotations

import contextlib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anyio
import anyio.from_thread
import anyio.lowlevel

from wacli_mcp.settings import Settings
from wacli_mcp.transcription import Transcriber, Transcript
from wacli_mcp.wacli import Wacli, WacliError

# Python's built-in table, without the Windows registry: the registry maps image/jpeg to `.jfif`,
# which some agents' file readers don't recognize as an image.
_MIME_TYPES = mimetypes.MimeTypes()
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_MANIFEST = ".media.json"
# Distinct files waiting for or in transcription. Each holds a thread until the model reaches it.
MAX_TRANSCRIPTION_JOBS = 8
# Kept as a folder name as is: uppercase, so it can't collide with another id on a case-insensitive disk.
_PLAIN_ENTRY = re.compile(r"^[A-Z0-9_-]{1,128}$")
# Names Windows reserves for devices, with or without an extension.
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$", re.IGNORECASE)


class MediaError(Exception):
    """An attachment could not be downloaded or transcribed. The message is meant for the agent."""


class TranscriptionUnavailable(MediaError):
    """No transcript is saved for this file, and this server cannot make one."""


@dataclass
class _Job:
    done: anyio.Event = field(default_factory=anyio.Event)
    result: Transcript | None = None
    error: str | None = None


@dataclass(frozen=True)
class MediaFile:
    path: Path
    media_type: str
    mime_type: str
    bytes: int

    @property
    def base_mime(self) -> str:
        return self.mime_type.split(";")[0].strip().lower()

    @property
    def is_audio_or_video(self) -> bool:
        return self.media_type in {"audio", "video"} or self.base_mime.startswith(("audio/", "video/"))


def default_media_dir() -> Path:
    """The per-user cache directory: `%LOCALAPPDATA%`, `~/Library/Caches`, or `$XDG_CACHE_HOME`."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "wacli-mcp" / "media"


def extension_for(mime_type: str, fallback_suffix: str) -> str:
    base = mime_type.split(";")[0].strip().lower()
    extension = _EXTENSIONS.get(base) or _MIME_TYPES.guess_extension(base, strict=False)
    if extension:
        return extension
    return fallback_suffix.lower() if _SAFE_SUFFIX.fullmatch(fallback_suffix) else ""


def _entry_name(value: str) -> str:
    """A folder name for a chat JID or message id, unique for each exact value, even ignoring case.

    WhatsApp message ids are usually uppercase hex and are used as is. Any other value becomes
    `~<value with unsafe characters replaced>-<digest of the original>`. Plain names never contain
    `~`, so the two kinds can't meet, and the digest separates values that differ only in replaced
    characters (`A.B`, `A_B`) or in case (`ab`, `AB`) on Windows and macOS disks.
    """
    if _PLAIN_ENTRY.fullmatch(value) and not _WINDOWS_RESERVED.fullmatch(value):
        return value
    return f"~{_SAFE_NAME.sub('_', value)}-{hashlib.sha256(value.encode()).hexdigest()[:10]}"


def _file_stem(message_id: str) -> str:
    """The attachment's file name without extension: only letters, digits, `_` and `-`, never dot-named."""
    stem = _SAFE_NAME.sub("_", message_id)
    return f"_{stem}" if _WINDOWS_RESERVED.fullmatch(stem) else stem


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write atomically, so a reader never sees half a file, even if the server is stopped mid-write."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _load_transcript(path: Path) -> Transcript | None:
    data = _read_json(path)
    if not data:
        return None
    try:
        return Transcript(**data)
    except TypeError:
        return None


def _explain(error: str) -> str:
    lowered = error.lower()
    if "no rows in result set" in lowered:
        return "No message with this id in this chat. Check the chat and id with list_messages."
    if "no downloadable media" in lowered:
        return "This message has no attachment that wacli can download."
    if any(code in lowered for code in ("403", "404", "410")):
        return (
            f"{error}. WhatsApp's servers no longer have this file. `wacli media download --retry-on-phone` "
            "can ask your phone to upload it again."
        )
    return error


class MediaLibrary:
    """Downloads attachments and keeps their transcripts beside them.

    Layout: `<media dir>/store-<digest>/<chat>/<message id>/`. Each wacli store, which is one WhatsApp
    account, gets its own tree, so a path handed out for one account is never overwritten by another.
    Callers resolve the store once per tool call with `resolve_store` and pass it to every method, and
    every wacli command in that call runs with that `--store`, so a change of wacli's default account
    can't mix two accounts in one tree.
    Each message folder holds the attachment, named `<message id><ext>`, and dot-named metadata: the
    `.media.json` manifest, which records the exact chat, message and store, and `.transcript-*.json`
    files. Attachment names never start with a dot, so the two never collide.
    """

    def __init__(self, settings: Settings, wacli: Wacli, transcriber: Transcriber) -> None:
        self.settings = settings
        self.wacli = wacli
        self.transcriber = transcriber
        self.root = (settings.media_dir or default_media_dir()).expanduser().resolve()
        # Transcriptions in progress, by transcript path. A request for a file that is already being
        # transcribed, such as a retry after a client timeout, waits for the same job.
        self._jobs: dict[Path, _Job] = {}

    async def resolve_store(self) -> str:
        """The store folder wacli uses right now, which `WACLI_STORE_DIR` or a default account can select.

        Returned as the real path, with links resolved and the case kept: the same string is passed to
        wacli as `--store` and keys the cache. Case is kept because Windows folders can be case-sensitive,
        so `Team` and `team` may be different stores.
        """
        if self.settings.store and not self.settings.account:
            return os.path.realpath(self.settings.store)
        try:
            doctor = await self.wacli.run(["doctor"])
        except WacliError as exc:
            raise MediaError(f"could not ask wacli which store it uses: {exc}") from exc
        store = doctor.get("store_dir") if isinstance(doctor, dict) else None
        if not isinstance(store, str) or not store:
            raise MediaError("wacli did not report its store folder")
        return os.path.realpath(store)

    def _folder(self, store: str, chat_jid: str, message_id: str) -> tuple[Path, dict[str, Any]]:
        store_root = (self.root / f"store-{hashlib.sha256(store.encode()).hexdigest()[:12]}").resolve()
        folder = (store_root / _entry_name(chat_jid) / _entry_name(message_id)).resolve()
        if folder.parent.parent != store_root:
            raise MediaError("invalid chat or message id")
        return folder, {"chat_jid": chat_jid, "message_id": message_id, "store_dir": store}

    def saved(self, store: str, chat_jid: str, message_id: str) -> MediaFile | None:
        """The attachment downloaded earlier for exactly this message and wacli store, if any."""
        folder, identity = self._folder(store, chat_jid, message_id)
        manifest = _read_json(folder / _MANIFEST)
        if not manifest or manifest.get("identity") != identity:
            return None
        name = manifest.get("file")
        if not isinstance(name, str) or name.startswith("."):
            return None
        path = folder / name
        try:
            if path.parent != folder or not path.is_file():
                return None
            size = path.stat().st_size
        except OSError:
            return None
        return MediaFile(
            path=path,
            media_type=str(manifest.get("media_type") or ""),
            mime_type=str(manifest.get("mime_type") or ""),
            bytes=size,
        )

    async def fetch(self, store: str, chat_jid: str, message_id: str) -> MediaFile:
        """Download one message's attachment, or return the copy downloaded earlier."""
        saved = self.saved(store, chat_jid, message_id)
        if saved:
            return saved
        folder, identity = self._folder(store, chat_jid, message_id)
        try:
            # Attachments are private: keep the folder readable by this user only, where the OS supports it.
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            folder.mkdir(parents=True, exist_ok=True)
            # wacli writes into an empty staging folder; the file moves into place only once it is complete.
            staging = Path(tempfile.mkdtemp(prefix=".download-", dir=folder)).resolve()
        except OSError as exc:
            raise MediaError(f"could not create the media folder: {exc}") from exc
        try:
            try:
                data = await self.wacli.run(
                    ["media", "download", "--chat", chat_jid, "--id", message_id, "--output", str(staging)],
                    store=store,
                )
            except WacliError as exc:
                raise MediaError(_explain(str(exc))) from exc
            if not isinstance(data, dict) or not isinstance(data.get("path"), str):
                raise MediaError("wacli did not say where it saved the file")
            downloaded = Path(data["path"]).resolve()
            if downloaded.parent != staging or not downloaded.is_file():
                raise MediaError("wacli saved the file somewhere other than the requested folder")
            mime_type = str(data.get("mime_type") or "")
            media_type = str(data.get("media_type") or "")
            target = folder / f"{_file_stem(message_id)}{extension_for(mime_type, downloaded.suffix)}"
            os.replace(downloaded, target)
            _write_json(
                folder / _MANIFEST,
                {"identity": identity, "file": target.name, "media_type": media_type, "mime_type": mime_type},
            )
            return MediaFile(path=target, media_type=media_type, mime_type=mime_type, bytes=target.stat().st_size)
        except OSError as exc:
            raise MediaError(f"could not save the attachment: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def transcript_path(self, media: MediaFile) -> Path:
        return media.path.with_name(f".transcript-{self.transcriber.cache_tag}.json")

    async def transcribe(self, media: MediaFile) -> Transcript:
        """Return the transcript of an audio or video attachment, making it if none is saved.

        Transcription runs in its own thread, which finishes and saves the transcript even if every
        caller stops waiting. A later request for the same file joins the running job.

        Raises:
            TranscriptionUnavailable: when no transcript is saved and the transcription extra is missing.
            MediaError: when the file cannot be decoded or transcribed, or too many jobs are running.
        """
        cache = self.transcript_path(media)
        saved = _load_transcript(cache)
        if saved:
            return saved
        job = self._jobs.get(cache)
        if job is None:
            reason = self.transcriber.unavailable_reason()
            if reason:
                raise TranscriptionUnavailable(reason)
            if len(self._jobs) >= MAX_TRANSCRIPTION_JOBS:
                raise MediaError(
                    f"{len(self._jobs)} transcriptions are already running. Ask for this one again once they finish."
                )
            job = self._jobs[cache] = _Job()
            try:
                threading.Thread(
                    target=self._run_job,
                    args=(cache, media, job, anyio.lowlevel.current_token()),
                    name="wacli-mcp-transcribe",
                    daemon=True,
                ).start()
            except RuntimeError as exc:
                del self._jobs[cache]
                raise MediaError(f"could not start transcribing: {exc}") from exc
        await job.done.wait()
        if job.result is None:
            raise MediaError(job.error or "transcription stopped without a result")
        return job.result

    def _run_job(self, cache: Path, media: MediaFile, job: _Job, token: anyio.lowlevel.EventLoopToken) -> None:
        try:
            # Another server process sharing the folder may have saved it meanwhile.
            job.result = _load_transcript(cache) or self.transcriber.transcribe(media.path)
            # If saving fails, the text is still returned, without a `transcript_file`.
            with contextlib.suppress(OSError):
                _write_json(cache, asdict(job.result))
        # Decoder and model errors have many types; each becomes an error on its own item.
        except Exception as exc:  # noqa: BLE001
            job.error = f"could not transcribe this file: {exc}"
        finally:
            # Always release the job, or every later request for this file would wait forever.
            # The event loop is gone only when the server is shutting down; nobody is waiting then.
            with contextlib.suppress(RuntimeError):
                anyio.from_thread.run_sync(self._finish, cache, job, token=token)

    def _finish(self, cache: Path, job: _Job) -> None:
        self._jobs.pop(cache, None)
        job.done.set()
