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
_TRUNCATED_MARK = " [...]"

Progress = Callable[[int, int, str], Awaitable[None]]


def to_json(payload: dict[str, Any]) -> str:
    """How every tool result is serialized. `fit_transcripts` measures with the same function."""
    return json.dumps(payload, ensure_ascii=False, indent=1)


def fit_transcripts(payload: dict[str, Any], entries: list[dict[str, Any]], limit: int) -> None:
    """Shorten the longest transcripts in `entries` until `to_json(payload)` fits in `limit` characters.

    Cutting the rendered JSON instead would break it and drop every later item. All transcripts are
    capped at one length, the largest that fits, so short ones stay whole. Shortened entries get
    `transcript_truncated`; their `transcript_file`, when present, has the full text.
    """
    full = {id(entry): entry["transcript"] for entry in entries if isinstance(entry.get("transcript"), str)}
    if not full or len(to_json(payload)) <= limit:
        return

    def cap_at(length: int) -> None:
        for entry in entries:
            text = full.get(id(entry))
            if text is None:
                continue
            if len(text) > length:
                entry["transcript"] = text[:length] + _TRUNCATED_MARK
                entry["transcript_truncated"] = True
            else:
                entry["transcript"] = text
                entry.pop("transcript_truncated", None)

    low, high, best = 0, max(len(text) for text in full.values()), 0
    while low <= high:
        middle = (low + high) // 2
        cap_at(middle)
        if len(to_json(payload)) <= limit:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    # If even empty transcripts don't fit, the rest of the result is too large on its own, and
    # rendering cuts it as it would any other oversized result.
    cap_at(best)


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
                await self._add_transcript(item, media, result.notes)
        return result

    async def add_voice_transcripts(self, messages: list[dict[str, Any]]) -> dict[str, str]:
        """Add a `transcript` to the newest voice notes in a page of messages. Returns notes for the result."""
        notes: dict[str, str] = {}
        audio = [m for m in messages if m.get("media_type") == "audio"]
        unavailable = self.library.transcriber.unavailable_reason() if audio else None
        for message in audio[:MAX_PER_CALL]:
            chat_jid, message_id = message.get("chat_jid"), message.get("id")
            if not isinstance(chat_jid, str) or not isinstance(message_id, str):
                continue
            try:
                media = await self.library.saved(chat_jid, message_id)
                if media is None:
                    if unavailable:
                        # Nothing saved and no way to transcribe: don't download a file that can't be used.
                        notes["transcription_unavailable"] = unavailable
                        continue
                    media = await self.library.fetch(chat_jid, message_id)
            except MediaError as exc:
                message["transcript_error"] = str(exc)
                continue
            await self._add_transcript(message, media, notes)
        if len(audio) > MAX_PER_CALL:
            notes["transcripts_note"] = (
                f"Only the newest {MAX_PER_CALL} voice notes were transcribed. "
                "Use `before` to page back, or get_media for specific messages."
            )
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

    async def _add_transcript(self, entry: dict[str, Any], media: MediaFile, notes: dict[str, str]) -> None:
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
        saved = self.library.transcript_path(media)
        if saved.is_file():
            entry["transcript_file"] = str(saved)
