from __future__ import annotations

import base64
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from mcp.types import CallToolResult, ImageContent, TextContent

from wacli_mcp.media import extension_for
from wacli_mcp.settings import Settings
from wacli_mcp.transcription import INSTALL_HINT, WhisperTranscriber, decode_audio

from support import JID, Calls, FakeTranscriber, make_server, text_of

pytestmark = pytest.mark.anyio


def items_of(result: CallToolResult) -> dict[str, dict[str, Any]]:
    assert not result.is_error, text_of(result)
    return {item["id"]: item for item in json.loads(text_of(result))["items"]}


def downloads(log: list[list[str]]) -> list[list[str]]:
    return [argv for argv in log if "download" in argv]


async def get_media(ids: list[str], *, transcriber: FakeTranscriber | None = None, **arguments: Any) -> CallToolResult:
    async with Client(make_server(sync_before_read=False, transcriber=transcriber)) as client:
        return await client.call_tool("get_media", {"chat": JID, "message_ids": ids, **arguments})


# Downloads


async def test_downloads_read_only_with_readable_names(calls: Calls, media_dir: Path) -> None:
    result = await get_media(["PHOTO1", "DOC1", "STICKER1", "VOICE1"])
    items = items_of(result)

    paths = {message_id: Path(item["path"]) for message_id, item in items.items()}
    assert paths["PHOTO1"].name == "PHOTO1.jpg", "image/jpeg must not keep wacli's .jfif name"
    assert paths["DOC1"].name == "DOC1.pdf"
    assert paths["STICKER1"].name == "STICKER1.webp"
    assert paths["VOICE1"].name == "VOICE1.ogg"
    for path in paths.values():
        assert path.is_file() and media_dir.resolve() in path.parents
    assert items["DOC1"]["mime_type"] == "application/pdf"
    assert items["VOICE1"]["media_type"] == "audio"
    assert not list(media_dir.rglob(".download-*")), "staging folders must be removed"

    for argv in downloads(calls()):
        assert "--read-only" in argv
        assert argv[argv.index("--chat") + 1] == JID


async def test_photos_are_shown_inline() -> None:
    result = await get_media(["PHOTO1", "STICKER1"])
    items = items_of(result)
    assert items["PHOTO1"]["shown_inline"] is True
    assert "shown_inline" not in items["STICKER1"], "stickers are WebP, which not every client renders"

    caption, image = result.content[1:]
    assert isinstance(caption, TextContent) and caption.text == "Photo from message PHOTO1:"
    assert isinstance(image, ImageContent) and image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data) == Path(items["PHOTO1"]["path"]).read_bytes()


async def test_large_or_fake_photos_are_not_inlined(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WACLI_MCP_INLINE_IMAGE_MAX_BYTES", "500")
    result = await get_media(["PHOTO1", "BIGPHOTO1", "FAKEPHOTO1"])
    items = items_of(result)
    assert items["PHOTO1"].get("shown_inline") is True
    assert "shown_inline" not in items["BIGPHOTO1"]
    assert "shown_inline" not in items["FAKEPHOTO1"], "a file that is not really a JPEG must not be inlined"
    assert len(result.content) == 3


async def test_downloads_are_reused(calls: Calls) -> None:
    first = items_of(await get_media(["PHOTO1", "VOICE1"]))
    second = items_of(await get_media(["VOICE1", "PHOTO1", "PHOTO1"]))
    assert first == second
    assert len(downloads(calls())) == 2


async def test_one_bad_item_does_not_fail_the_batch() -> None:
    items = items_of(await get_media(["EXPIRED1", "TEXTONLY1", "PHOTO1"]))
    assert "410" in items["EXPIRED1"]["error"] and "--retry-on-phone" in items["EXPIRED1"]["error"]
    assert "no attachment" in items["TEXTONLY1"]["error"]
    assert "path" in items["PHOTO1"]


async def test_file_outside_the_download_folder_is_refused(media_dir: Path) -> None:
    for _ in range(2):
        items = items_of(await get_media(["ESCAPE1"]))
        assert "somewhere other than" in items["ESCAPE1"]["error"]
        assert "path" not in items["ESCAPE1"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"chat": "Ada", "message_ids": ["PHOTO1"]},
        {"chat": "--help", "message_ids": ["PHOTO1"]},
        {"chat": JID, "message_ids": ["../../etc"]},
        {"chat": JID, "message_ids": ["PHOTO1\n--output"]},
        {"chat": JID, "message_ids": []},
        {"chat": JID, "message_ids": [f"PHOTO{n}" for n in range(21)]},
    ],
)
async def test_invalid_input_is_refused(calls: Calls, arguments: dict[str, Any]) -> None:
    async with Client(make_server(sync_before_read=False)) as client:
        result = await client.call_tool("get_media", arguments)
    assert result.is_error
    assert downloads(calls()) == []


async def test_phone_number_chat_becomes_a_jid(calls: Calls) -> None:
    async with Client(make_server(sync_before_read=False)) as client:
        await client.call_tool("get_media", {"chat": "+1 555 123 4567", "message_ids": ["PHOTO1"]})
    (argv,) = downloads(calls())
    assert argv[argv.index("--chat") + 1] == JID


@pytest.mark.parametrize(
    ("mime_type", "suffix", "expected"),
    [
        ("image/jpeg", ".jfif", ".jpg"),
        ("audio/ogg; codecs=opus", ".oga", ".ogg"),
        ("application/pdf", ".pdf", ".pdf"),
        ("application/x-unknown-kind", ".BLOB", ".blob"),
        ("application/x-unknown-kind", ".a b", ""),
        ("", "", ""),
    ],
)
def test_extension_for(mime_type: str, suffix: str, expected: str) -> None:
    assert extension_for(mime_type, suffix) == expected


# Transcription


async def test_transcribes_audio_and_video_once() -> None:
    transcriber = FakeTranscriber()
    first = items_of(await get_media(["VOICE1", "CLIP1", "PHOTO1"], transcriber=transcriber, transcribe=True))
    assert first["VOICE1"]["transcript"] == "words from VOICE1"
    assert first["VOICE1"]["transcript_language"] == "es"
    assert first["VOICE1"]["duration_seconds"] == 12.5
    assert first["CLIP1"]["transcript"] == "words from CLIP1"
    assert "transcript" not in first["PHOTO1"]

    second = items_of(await get_media(["VOICE1", "CLIP1"], transcriber=transcriber, transcribe=True))
    assert second["VOICE1"]["transcript"] == "words from VOICE1"
    assert len(transcriber.calls) == 2, "a second request must reuse the saved transcript"


async def test_no_transcription_unless_asked() -> None:
    transcriber = FakeTranscriber()
    items = items_of(await get_media(["VOICE1"], transcriber=transcriber))
    assert "transcript" not in items["VOICE1"]
    assert transcriber.calls == []


async def test_missing_transcription_extra_still_returns_files() -> None:
    transcriber = FakeTranscriber(reason=INSTALL_HINT)
    result = await get_media(["VOICE1"], transcriber=transcriber, transcribe=True)
    payload = json.loads(text_of(result))
    assert payload["transcription_unavailable"] == INSTALL_HINT
    assert Path(payload["items"][0]["path"]).is_file()
    assert transcriber.calls == []


async def test_transcription_failure_is_an_item_error() -> None:
    transcriber = FakeTranscriber(fail="CUDA out of memory")
    items = items_of(await get_media(["VOICE1", "PHOTO1"], transcriber=transcriber, transcribe=True))
    assert "CUDA out of memory" in items["VOICE1"]["transcript_error"]
    assert "path" in items["VOICE1"] and "path" in items["PHOTO1"]


async def test_list_messages_adds_voice_note_transcripts(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    monkeypatch.setenv("FAKE_WACLI_VOICE_NOTES", "2")
    transcriber = FakeTranscriber()
    async with Client(make_server(sync_before_read=False, transcriber=transcriber)) as client:
        plain = json.loads(text_of(await client.call_tool("list_messages", {"chat": JID})))
        assert downloads(calls()) == [], "voice notes are only downloaded when asked"
        result = await client.call_tool("list_messages", {"chat": JID, "transcribe_audio": True})
    assert all("transcript" not in m for m in plain["result"]["messages"])
    messages = {m["id"]: m for m in json.loads(text_of(result))["result"]["messages"]}
    assert messages["VOICE0"]["transcript"] == "words from VOICE0"
    assert messages["VOICE1"]["transcript"] == "words from VOICE1"
    assert "transcript" not in messages["MSG1"]


async def test_list_messages_caps_transcripts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_WACLI_VOICE_NOTES", "21")
    transcriber = FakeTranscriber()
    async with Client(make_server(sync_before_read=False, transcriber=transcriber)) as client:
        result = await client.call_tool("list_messages", {"chat": JID, "limit": 50, "transcribe_audio": True})
    payload = json.loads(text_of(result))
    transcribed = [m for m in payload["result"]["messages"] if "transcript" in m]
    assert len(transcribed) == 20
    assert "newest 20" in payload["transcripts_note"]


async def test_list_messages_reports_missing_extra(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    monkeypatch.setenv("FAKE_WACLI_VOICE_NOTES", "1")
    transcriber = FakeTranscriber(reason=INSTALL_HINT)
    async with Client(make_server(sync_before_read=False, transcriber=transcriber)) as client:
        result = await client.call_tool("list_messages", {"chat": JID, "transcribe_audio": True})
    assert json.loads(text_of(result))["transcription_unavailable"] == INSTALL_HINT
    assert downloads(calls()) == []


async def test_status_reports_media_and_transcription(media_dir: Path) -> None:
    async with Client(make_server(transcriber=FakeTranscriber(reason=INSTALL_HINT))) as client:
        payload = json.loads(text_of(await client.call_tool("whatsapp_status", {})))
    assert payload["media_dir"] == str(media_dir.resolve())
    assert payload["transcription"] == {"available": False}


def test_whisper_reports_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "faster_whisper" else real_find_spec(name, *a)
    )
    transcriber = WhisperTranscriber(Settings())
    assert transcriber.unavailable_reason() == INSTALL_HINT
    status = transcriber.status()
    assert status["available"] is False and status["how_to_enable"] == INSTALL_HINT


def test_cache_tag_names_model_and_language() -> None:
    assert WhisperTranscriber(Settings()).cache_tag == "large-v3-turbo-auto"
    tagged = WhisperTranscriber(Settings(transcribe_model="C:/models/my whisper", transcribe_language="es"))
    assert tagged.cache_tag == "C-models-my-whisper-es"


def test_decode_audio_resamples_to_16k_mono(tmp_path: Path) -> None:
    """Runs only where the `transcribe` extra is installed."""
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    path = tmp_path / "tone.wav"
    rate = 44_100
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = (int(8000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(rate))
        wav.writeframes(b"".join(struct.pack("<hh", sample, sample) for sample in frames))
    audio = decode_audio(path)
    assert str(audio.dtype) == "float32"
    assert abs(len(audio) - 16_000) < 200
    assert 0.2 < float(abs(audio).max()) < 0.3


@pytest.mark.parametrize(
    ("error", "falls_back"),
    [("Library cublas64_12.dll is not found or cannot be loaded", True), ("unsupported sample format", False)],
)
def test_whisper_falls_back_to_cpu_only_for_gpu_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: str, falls_back: bool
) -> None:
    from types import SimpleNamespace

    from wacli_mcp import transcription

    created: list[str] = []

    class FakeModel:
        def __init__(self, name: str, device: str, compute_type: str) -> None:
            self.device = device
            created.append(f"{device}/{compute_type}")

        def transcribe(self, audio: Any, language: str | None, vad_filter: bool) -> Any:
            if self.device == "cuda":
                raise RuntimeError(error)
            return iter([SimpleNamespace(text=" hola ")]), SimpleNamespace(language="es", duration=3.04)

    real_import = transcription.importlib.import_module
    fake_module = SimpleNamespace(WhisperModel=FakeModel)
    monkeypatch.setattr(
        transcription.importlib,
        "import_module",
        lambda name, *a: fake_module if name == "faster_whisper" else real_import(name, *a),
    )
    monkeypatch.setattr(transcription, "decode_audio", lambda path: [0.0] * 160)
    monkeypatch.setattr(transcription, "_cuda_device_count", lambda: 1)
    monkeypatch.setattr(transcription, "_expose_cuda_libraries", lambda: None)

    transcriber = WhisperTranscriber(Settings())
    if not falls_back:
        with pytest.raises(RuntimeError, match=error):
            transcriber.transcribe(tmp_path / "note.ogg")
        assert created == ["cuda/float16"]
        return
    transcript = transcriber.transcribe(tmp_path / "note.ogg")
    assert transcript.text == "hola" and transcript.device == "cpu" and transcript.duration_seconds == 3.0
    assert created == ["cuda/float16", "cpu/int8"]
    assert "cublas64_12.dll" in transcriber.status()["gpu_error"]
