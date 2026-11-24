"""Downloads message attachments with wacli and transcribes audio, caching both on disk."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
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
_METADATA = "media.json"


class MediaError(Exception):
    """An attachment could not be downloaded or transcribed. The message is meant for the agent."""


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


def _safe_name(value: str) -> str:
    return _SAFE_NAME.sub("_", value)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write atomically, so a reader never sees half a file, even if the server is stopped mid-write."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
    """Keeps downloaded attachments in `<media dir>/<chat>/<message id>/`, with their transcripts beside them."""

    def __init__(self, settings: Settings, wacli: Wacli, transcriber: Transcriber) -> None:
        self.settings = settings
        self.wacli = wacli
        self.transcriber = transcriber
        self.root = (settings.media_dir or default_media_dir()).expanduser().resolve()

    def _folder(self, chat_jid: str, message_id: str) -> Path:
        folder = (self.root / _safe_name(chat_jid) / _safe_name(message_id)).resolve()
        if folder.parent.parent != self.root:
            raise MediaError("invalid chat or message id")
        return folder

    def _make_root(self) -> None:
        # Attachments are private: keep the folder readable by this user only, where the OS supports it.
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _cached(self, folder: Path) -> MediaFile | None:
        info = _read_json(folder / _METADATA)
        if not info or not isinstance(info.get("file"), str):
            return None
        path = folder / info["file"]
        if path.parent != folder or not path.is_file():
            return None
        return MediaFile(
            path=path,
            media_type=str(info.get("media_type") or ""),
            mime_type=str(info.get("mime_type") or ""),
            bytes=path.stat().st_size,
        )

    async def fetch(self, chat_jid: str, message_id: str) -> MediaFile:
        """Download one message's attachment, or return the copy downloaded earlier."""
        folder = self._folder(chat_jid, message_id)
        cached = self._cached(folder)
        if cached:
            return cached
        self._make_root()
        folder.mkdir(parents=True, exist_ok=True)
        # wacli writes into an empty staging folder; the file moves into place only once it is complete.
        staging = Path(tempfile.mkdtemp(prefix=".download-", dir=folder)).resolve()
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
            target = folder / f"{_safe_name(message_id)}{extension_for(mime_type, downloaded.suffix)}"
            os.replace(downloaded, target)
            media = MediaFile(
                path=target,
                media_type=str(data.get("media_type") or ""),
                mime_type=mime_type,
                bytes=target.stat().st_size,
            )
            _write_json(
                folder / _METADATA,
                {"file": target.name, "media_type": media.media_type, "mime_type": media.mime_type},
            )
            return media
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def transcribe(self, media: MediaFile) -> Transcript:
        """Transcribe an audio or video attachment, or return the transcript made earlier with the same model."""
        cache = media.path.with_name(f"transcript-{self.transcriber.cache_tag}.json")
        cached = _read_json(cache)
        if cached:
            try:
                return Transcript(**cached)
            except TypeError:
                pass

        def work() -> Transcript:
            try:
                transcript = self.transcriber.transcribe(media.path)
            # Decoder and model errors have many types; each becomes an error on its own item.
            except Exception as exc:
                raise MediaError(f"could not transcribe this file: {exc}") from exc
            # Written by the worker, so the transcript is kept even if the client stops waiting.
            _write_json(cache, asdict(transcript))
            return transcript

        # The worker keeps running if the tool call is cancelled, for example by a client timeout,
        # so asking again later returns the cached transcript instead of starting over.
        return await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
