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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anyio.to_thread

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
_MANIFEST = "media.json"
_TRANSCRIPTS = "transcript-*.json"


class MediaError(Exception):
    """An attachment could not be downloaded or transcribed. The message is meant for the agent."""


class TranscriptionUnavailable(MediaError):
    """No transcript is saved for this file, and this server cannot make one."""


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
    """A folder name for a chat JID or message id: the value itself when it is a safe file name.

    Values that need changing get a digest of the original, so two ids that differ only in characters
    replaced here, such as `A:B` and `A_B`, never share a folder.
    """
    safe = _SAFE_NAME.sub("_", value)
    if safe == value:
        return value
    return f"{safe}-{hashlib.sha256(value.encode()).hexdigest()[:10]}"


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
    """Downloads attachments into `<media dir>/<chat>/<message id>/` and keeps their transcripts beside them.

    Each folder has a manifest naming the exact chat, message, wacli account and store it came from.
    A saved file or transcript is reused only when all of them match the request.
    """

    def __init__(self, settings: Settings, wacli: Wacli, transcriber: Transcriber) -> None:
        self.settings = settings
        self.wacli = wacli
        self.transcriber = transcriber
        self.root = (settings.media_dir or default_media_dir()).expanduser().resolve()
        # One worker per transcript file at a time. A retry after a client timeout waits for the
        # worker that is still running and then reads its result, instead of transcribing again.
        self._transcript_locks: dict[Path, threading.Lock] = {}
        self._transcript_locks_guard = threading.Lock()

    def _identity(self, chat_jid: str, message_id: str) -> dict[str, Any]:
        return {
            "chat_jid": chat_jid,
            "message_id": message_id,
            "account": self.settings.account,
            "store": self.settings.store,
        }

    def _folder(self, chat_jid: str, message_id: str) -> Path:
        folder = (self.root / _entry_name(chat_jid) / _entry_name(message_id)).resolve()
        if folder.parent.parent != self.root:
            raise MediaError("invalid chat or message id")
        return folder

    def saved(self, chat_jid: str, message_id: str) -> MediaFile | None:
        """The attachment downloaded earlier for exactly this message and wacli account, if any."""
        folder = self._folder(chat_jid, message_id)
        manifest = _read_json(folder / _MANIFEST)
        if not manifest or manifest.get("identity") != self._identity(chat_jid, message_id):
            return None
        name = manifest.get("file")
        if not isinstance(name, str):
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

    async def fetch(self, chat_jid: str, message_id: str) -> MediaFile:
        """Download one message's attachment, or return the copy downloaded earlier."""
        saved = self.saved(chat_jid, message_id)
        if saved:
            return saved
        folder = self._folder(chat_jid, message_id)
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
                    ["media", "download", "--chat", chat_jid, "--id", message_id, "--output", str(staging)]
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
            target = folder / f"{_SAFE_NAME.sub('_', message_id)}{extension_for(mime_type, downloaded.suffix)}"
            os.replace(downloaded, target)
            # Transcripts in this folder belong to whatever was here before, which may be another message.
            for old in folder.glob(_TRANSCRIPTS):
                old.unlink(missing_ok=True)
            _write_json(
                folder / _MANIFEST,
                {
                    "identity": self._identity(chat_jid, message_id),
                    "file": target.name,
                    "media_type": media_type,
                    "mime_type": mime_type,
                },
            )
            return MediaFile(path=target, media_type=media_type, mime_type=mime_type, bytes=target.stat().st_size)
        except OSError as exc:
            raise MediaError(f"could not save the attachment: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def transcript_path(self, media: MediaFile) -> Path:
        return media.path.with_name(f"transcript-{self.transcriber.cache_tag}.json")

    def _transcript_lock(self, path: Path) -> threading.Lock:
        with self._transcript_locks_guard:
            return self._transcript_locks.setdefault(path, threading.Lock())

    async def transcribe(self, media: MediaFile) -> Transcript:
        """Return the transcript of an audio or video attachment, making it if none is saved.

        Raises:
            TranscriptionUnavailable: when no transcript is saved and the transcription extra is missing.
            MediaError: when the file cannot be decoded or transcribed.
        """
        cache = self.transcript_path(media)
        saved = _load_transcript(cache)
        if saved:
            return saved
        reason = self.transcriber.unavailable_reason()
        if reason:
            raise TranscriptionUnavailable(reason)

        def work() -> Transcript:
            with self._transcript_lock(cache):
                # A worker abandoned by an earlier, cancelled call may have finished while this one waited.
                saved = _load_transcript(cache)
                if saved:
                    return saved
                try:
                    transcript = self.transcriber.transcribe(media.path)
                # Decoder and model errors have many types; each becomes an error on its own item.
                except Exception as exc:
                    raise MediaError(f"could not transcribe this file: {exc}") from exc
                # Saved by the worker, so the transcript is kept even if the client stops waiting.
                # If saving fails, the text is still returned; asking again transcribes again.
                with contextlib.suppress(OSError):
                    _write_json(cache, asdict(transcript))
                return transcript

        # The worker keeps running if the tool call is cancelled, for example by a client timeout,
        # so asking again waits for it and returns its result instead of starting over.
        return await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
