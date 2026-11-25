"""Turns attachments and transcripts into tool results: one entry per message, inline photos, size limits.

`MediaLibrary` owns downloading and caching. This module decides what a result shows, so the tool
handlers in `server.py` only validate input and render.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.types import ImageContent, TextContent

from wacli_mcp.media import MediaError, MediaFile, MediaLibrary, TranscriptionUnavailable
from wacli_mcp.settings import Settings

MAX_PER_CALL = 20
# Inline only formats every client renders, and only files that start like one. A broken image in a
# tool result can make the model provider reject the whole request.
_IMAGE_SIGNATURES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
# All inline photos in one result together may use this many times the per-photo limit.
_INLINE_TOTAL_FACTOR = 4
# Room kept for ids, paths, notices and JSON escaping when transcripts are shortened to fit.
_RESERVED_CHARS = 2_000
_MIN_TRANSCRIPT_CHARS = 500
_TRUNCATED_MARK = " [...]"

Progress = Callable[[int, int, str], Awaitable[None]]


@dataclass
class MediaResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    images: list[TextContent | ImageContent] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)


class Attachments:
    def __init__(self, library: MediaLibrary, settings: Settings) -> None:
        self.library = library
        self.settings = settings

    async def collect(
        self, chat_jid: str, message_ids: list[str], *, transcribe: bool, progress: Progress
    ) -> MediaResult:
        """Fetch each message's attachment. A failure is reported on its own item and doesn't stop the rest."""
        result = MediaResult()
        transcript_files: dict[int, str] = {}
        inline_left = self.settings.inline_image_max_bytes * _INLINE_TOTAL_FACTOR
        for done, message_id in enumerate(message_ids):
            await progress(done, len(message_ids), f"Fetching the attachment of message {message_id}")
            item: dict[str, Any] = {"id": message_id}
            result.items.append(item)
            try:
                media = await self.library.fetch(chat_jid, message_id)
            except MediaError as exc:
                item["error"] = str(exc)
                continue
            item.update(media_type=media.media_type, mime_type=media.mime_type, bytes=media.bytes, path=str(media.path))
            if self._inlinable(media):
                image = self._image(media) if media.bytes <= inline_left else None
                if image is not None:
                    inline_left -= media.bytes
                    item["shown_inline"] = True
                    result.images += [TextContent(type="text", text=f"Photo from message {message_id}:"), image]
                else:
                    result.notes["inline_note"] = (
                        "Some photos are not shown inline, to keep this result small. Open them from their `path`."
                    )
            if transcribe and media.is_audio_or_video:
                await self._add_transcript(item, media, result.notes, transcript_files)
        self._fit_transcripts(result.items, transcript_files)
        return result

    async def add_voice_transcripts(self, messages: list[dict[str, Any]]) -> dict[str, str]:
        """Add a `transcript` to the newest voice notes in a page of messages. Returns notes for the result."""
        notes: dict[str, str] = {}
        transcript_files: dict[int, str] = {}
        audio = [m for m in messages if m.get("media_type") == "audio"]
        unavailable = self.library.transcriber.unavailable_reason() if audio else None
        for message in audio[:MAX_PER_CALL]:
            chat_jid, message_id = message.get("chat_jid"), message.get("id")
            if not isinstance(chat_jid, str) or not isinstance(message_id, str):
                continue
            try:
                media = self.library.saved(chat_jid, message_id)
                if media is None:
                    if unavailable:
                        # Nothing saved and no way to transcribe: don't download a file that can't be used.
                        notes["transcription_unavailable"] = unavailable
                        continue
                    media = await self.library.fetch(chat_jid, message_id)
            except MediaError as exc:
                message["transcript_error"] = str(exc)
                continue
            await self._add_transcript(message, media, notes, transcript_files)
        if len(audio) > MAX_PER_CALL:
            notes["transcripts_note"] = (
                f"Only the newest {MAX_PER_CALL} voice notes were transcribed. "
                "Use `before` to page back, or get_media for specific messages."
            )
        self._fit_transcripts(messages, transcript_files)
        return notes

    def _inlinable(self, media: MediaFile) -> bool:
        return (
            media.media_type == "image"
            and media.base_mime in _IMAGE_SIGNATURES
            and media.bytes <= self.settings.inline_image_max_bytes
        )

    def _image(self, media: MediaFile) -> ImageContent | None:
        try:
            data = media.path.read_bytes()
        except OSError:
            return None
        if not data.startswith(_IMAGE_SIGNATURES[media.base_mime]):
            return None
        return ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type=media.base_mime)

    async def _add_transcript(
        self, entry: dict[str, Any], media: MediaFile, notes: dict[str, str], files: dict[int, str]
    ) -> None:
        try:
            transcript = await self.library.transcribe(media)
        except TranscriptionUnavailable as exc:
            notes["transcription_unavailable"] = str(exc)
            return
        except MediaError as exc:
            entry["transcript_error"] = str(exc)
            return
        entry["transcript"] = transcript.text
        if transcript.language:
            entry["transcript_language"] = transcript.language
        entry["duration_seconds"] = transcript.duration_seconds
        files[id(entry)] = str(self.library.transcript_path(media))

    def _fit_transcripts(self, entries: list[dict[str, Any]], files: dict[int, str]) -> None:
        """Shorten the longest transcripts so the rendered result stays under `max_output_chars`.

        Cutting the rendered JSON instead would break it and drop every later item. Shortened entries
        get `transcript_truncated` and `transcript_file`, the saved JSON with the full `text`.
        """
        with_text = sorted(
            (entry for entry in entries if isinstance(entry.get("transcript"), str)),
            key=lambda entry: len(entry["transcript"]),
        )
        if not with_text:
            return
        total = sum(len(entry["transcript"]) for entry in with_text)
        rendered = len(json.dumps(entries, ensure_ascii=False, indent=1))
        budget = self.settings.max_output_chars - _RESERVED_CHARS - (rendered - total)
        if total <= budget:
            return
        # Short transcripts keep their full text; the longest share what is left.
        for index, entry in enumerate(with_text):
            share = max(_MIN_TRANSCRIPT_CHARS, budget // (len(with_text) - index))
            text: str = entry["transcript"]
            if len(text) > share:
                entry["transcript"] = text[:share] + _TRUNCATED_MARK
                entry["transcript_truncated"] = True
                if id(entry) in files:
                    entry["transcript_file"] = files[id(entry)]
                text = entry["transcript"]
            budget -= len(text)
