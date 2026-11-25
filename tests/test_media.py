from __future__ import annotations

import base64
import json
import math
import os
import struct
import sys
import threading
import wave
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import pytest
from mcp.client import Client
from mcp.types import CallToolResult, ImageContent, TextContent

from wacli_mcp.attachments import fit_transcripts, to_json
from wacli_mcp.media import MediaError, MediaLibrary, extension_for
from wacli_mcp.settings import Settings
from wacli_mcp.transcription import INSTALL_HINT, Transcript, TranscriptionError, WhisperTranscriber, decode_audio
from wacli_mcp.wacli import Wacli

from fake_wacli import JPEG
from support import FAKE_WACLI, JID, Calls, FakeTranscriber, make_server, text_of

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


def test_cache_tag_is_readable_and_exact() -> None:
    def tag(**overrides: Any) -> str:
        return WhisperTranscriber(Settings(**overrides)).cache_tag

    assert tag().startswith("large-v3-turbo-auto-")
    assert tag(transcribe_model="C:/models/my whisper", transcribe_language="es").startswith("my-whisper-es-")
    # Model paths that shorten to the same readable name still get different tags.
    assert tag(transcribe_model="C:/a/my whisper") != tag(transcribe_model="C:/b/my whisper")
    assert tag(transcribe_language="es") != tag()


def write_tone(path: Path, seconds: float) -> Path:
    rate = 44_100
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = (int(8000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(int(rate * seconds)))
        wav.writeframes(b"".join(struct.pack("<hh", sample, sample) for sample in frames))
    return path


def test_decode_audio_resamples_to_16k_mono(tmp_path: Path) -> None:
    """Runs only where the `transcribe` extra is installed, as in the transcription CI job."""
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    audio = decode_audio(write_tone(tmp_path / "tone.wav", 1.0))
    assert str(audio.dtype) == "float32"
    assert abs(len(audio) - 16_000) < 200
    assert 0.2 < float(abs(audio).max()) < 0.3


def test_decode_audio_refuses_long_recordings(tmp_path: Path) -> None:
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    with pytest.raises(TranscriptionError, match="longer than"):
        decode_audio(write_tone(tmp_path / "tone.wav", 2.0), max_seconds=1.0)


def test_transcription_stack_imports() -> None:
    """Catches an extra whose native libraries install but don't load."""
    faster_whisper = pytest.importorskip("faster_whisper")
    ctranslate2 = pytest.importorskip("ctranslate2")
    assert ctranslate2.get_cuda_device_count() >= 0
    assert callable(faster_whisper.WhisperModel)


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


# Cache identity, failures and limits


async def test_ids_that_look_alike_on_disk_stay_separate(calls: Calls) -> None:
    items = items_of(await get_media(["PHOTO.1", "PHOTO_1"]))
    assert items["PHOTO.1"]["path"] != items["PHOTO_1"]["path"]
    assert len(downloads(calls())) == 2


async def test_cache_is_scoped_to_the_wacli_account(calls: Calls) -> None:
    async def fetch(account: str, transcriber: FakeTranscriber) -> dict[str, Any]:
        server = make_server(sync_before_read=False, account=account, transcriber=transcriber)
        async with Client(server) as client:
            result = await client.call_tool("get_media", {"chat": JID, "message_ids": ["VOICE1"], "transcribe": True})
        return items_of(result)["VOICE1"]

    first, second = FakeTranscriber(text="from account a"), FakeTranscriber(text="from account b")
    assert (await fetch("a", first))["transcript"] == "from account a"
    assert (await fetch("b", second))["transcript"] == "from account b", "account b must not see a's transcript"
    assert len(downloads(calls())) == 2
    assert (await fetch("b", second))["transcript"] == "from account b"
    assert len(downloads(calls())) == 2 and len(second.calls) == 1


class Blocking(FakeTranscriber):
    """Holds every transcription open until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.started, self.release = threading.Event(), threading.Event()

    def transcribe(self, path: Path) -> Transcript:
        self.calls.append(path)
        self.started.set()
        self.release.wait(10)
        return Transcript("done", "es", 1.0, "fake", "cpu")


def library_with(transcriber: FakeTranscriber) -> MediaLibrary:
    settings = Settings()
    return MediaLibrary(settings, Wacli(settings, command=(sys.executable, str(FAKE_WACLI))), transcriber)


def transcription_threads() -> int:
    return sum(thread.name == "wacli-mcp-transcribe" for thread in threading.enumerate())


async def test_retries_join_the_transcription_already_running() -> None:
    """Clients that time out and ask again must not start more transcriptions or more threads."""
    transcriber = Blocking()
    library = library_with(transcriber)
    media = await library.fetch(await library.resolve_store(), JID, "VOICE1")
    results: list[Transcript] = []

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            with anyio.move_on_after(0.1):  # each attempt gives up while the transcription runs
                await library.transcribe(media)
        await anyio.to_thread.run_sync(transcriber.started.wait)
        assert transcription_threads() == 1

        async def retry() -> None:
            results.append(await library.transcribe(media))

        tg.start_soon(retry)
        await anyio.sleep(0.1)
        transcriber.release.set()

    assert [r.text for r in results] == ["done"]
    assert len(transcriber.calls) == 1
    assert library.transcript_path(media).is_file()


async def test_too_many_running_transcriptions_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wacli_mcp.media.MAX_TRANSCRIPTION_JOBS", 1)
    transcriber = Blocking()
    library = library_with(transcriber)
    store = await library.resolve_store()
    first, second = await library.fetch(store, JID, "VOICE1"), await library.fetch(store, JID, "VOICE2")
    with anyio.move_on_after(0.1):
        await library.transcribe(first)
    with pytest.raises(MediaError, match="already running"):
        await library.transcribe(second)
    transcriber.release.set()
    assert (await library.transcribe(first)).text == "done"


async def test_unsaved_transcript_has_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from wacli_mcp import media

    real_write = media._write_json  # pyright: ignore[reportPrivateUsage]

    def failing_write(path: Path, data: dict[str, Any]) -> None:
        if path.name.startswith(".transcript-"):
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(media, "_write_json", failing_write)
    items = items_of(await get_media(["VOICE1"], transcriber=FakeTranscriber(), transcribe=True))
    assert items["VOICE1"]["transcript"] == "words from VOICE1"
    assert "transcript_file" not in items["VOICE1"]


async def test_attachment_names_never_clash_with_metadata(calls: Calls) -> None:
    """A document named like the manifest or a transcript must not overwrite it or be deleted."""
    first = items_of(await get_media(["media", "transcript-x"]))
    assert Path(first["media"]["path"]).read_bytes() == b'{"kind": "document"}'
    assert Path(first["transcript-x"]["path"]).is_file()
    assert items_of(await get_media(["media", "transcript-x"])) == first
    assert len(downloads(calls())) == 2


async def test_accounts_never_share_a_path() -> None:
    async def path_for(account: str) -> str:
        async with Client(make_server(sync_before_read=False, account=account)) as client:
            result = await client.call_tool("get_media", {"chat": JID, "message_ids": ["PHOTO1"]})
        return items_of(result)["PHOTO1"]["path"]

    assert await path_for("a") != await path_for("b")


async def test_full_batch_of_transcripts_stays_valid_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Twenty long transcripts under a long media folder, at the default output limit."""
    monkeypatch.setenv("WACLI_MCP_MEDIA_DIR", str(tmp_path / ("long-folder-name-" * 8) / "media"))
    transcriber = FakeTranscriber(text="una palabra bastante larga " * 400)
    result = await get_media([f"VOICE{n}" for n in range(20)], transcriber=transcriber, transcribe=True)
    text = text_of(result)
    assert len(text) <= Settings().max_output_chars
    items = items_of(result)
    assert len(items) == 20
    assert all(item["transcript_truncated"] and Path(item["transcript_file"]).is_file() for item in items.values())


async def test_disk_errors_become_item_errors(media_dir: Path) -> None:
    media_dir.parent.mkdir(parents=True, exist_ok=True)
    media_dir.write_text("not a folder", encoding="utf-8")
    items = items_of(await get_media(["PHOTO1", "VOICE1"]))
    assert all("could not create the media folder" in item["error"] for item in items.values())


async def test_long_transcripts_are_shortened_not_the_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WACLI_MCP_MAX_OUTPUT_CHARS", "20000")
    transcriber = FakeTranscriber(text="palabra " * 4000)
    result = await get_media(["VOICE1", "VOICE2", "VOICE3"], transcriber=transcriber, transcribe=True)
    assert len(text_of(result)) <= 20_000
    items = items_of(result)  # still valid JSON, with every item present
    assert set(items) == {"VOICE1", "VOICE2", "VOICE3"}
    for item in items.values():
        assert item["transcript_truncated"] is True and item["transcript"].endswith("[...]")
        saved = json.loads(Path(item["transcript_file"]).read_text(encoding="utf-8"))
        assert saved["text"] == transcriber.text


async def test_inline_photos_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WACLI_MCP_INLINE_IMAGE_MAX_BYTES", str(len(JPEG)))  # four photos fit in the total
    result = await get_media([f"PHOTO{n}" for n in range(1, 6)])
    payload = json.loads(text_of(result))
    shown = [item["id"] for item in payload["items"] if item.get("shown_inline")]
    assert shown == ["PHOTO1", "PHOTO2", "PHOTO3", "PHOTO4"]
    assert "not shown inline" in payload["inline_note"]
    assert sum(isinstance(block, ImageContent) for block in result.content) == 4


async def test_saved_transcripts_work_without_the_extra(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    await get_media(["VOICE0"], transcriber=FakeTranscriber(), transcribe=True)
    missing = FakeTranscriber(reason=INSTALL_HINT)

    items = items_of(await get_media(["VOICE0"], transcriber=missing, transcribe=True))
    assert items["VOICE0"]["transcript"] == "words from VOICE0"

    monkeypatch.setenv("FAKE_WACLI_VOICE_NOTES", "2")
    async with Client(make_server(sync_before_read=False, transcriber=missing)) as client:
        payload = json.loads(text_of(await client.call_tool("list_messages", {"chat": JID, "transcribe_audio": True})))
    messages = {m["id"]: m for m in payload["result"]["messages"]}
    assert messages["VOICE0"]["transcript"] == "words from VOICE0"
    assert "transcript" not in messages["VOICE1"]
    assert payload["transcription_unavailable"] == INSTALL_HINT
    assert len(downloads(calls())) == 1, "VOICE1 has no saved transcript and can't be transcribed: no download"
    assert missing.calls == []


async def test_one_server_follows_a_change_of_default_account(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    """wacli resolves its default account on every run; the cache must follow it, not the first answer."""
    async with Client(make_server(sync_before_read=False)) as client:

        async def path_as(account: str) -> str:
            monkeypatch.setenv("FAKE_WACLI_DEFAULT_ACCOUNT", account)
            result = await client.call_tool("get_media", {"chat": JID, "message_ids": ["PHOTO1"]})
            return items_of(result)["PHOTO1"]["path"]

        path_a, path_b = await path_as("a"), await path_as("b")
        assert path_a != path_b
        assert await path_as("a") == path_a

    pinned = downloads(calls())
    stores = [os.path.realpath(f"/fake/stores/{account}") for account in ("a", "b")]
    assert [argv[argv.index("--store") + 1] for argv in pinned] == stores
    assert all("--account" not in argv for argv in pinned)


async def test_transcribed_listing_reads_one_store(monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    monkeypatch.setenv("FAKE_WACLI_VOICE_NOTES", "1")
    async with Client(make_server(sync_before_read=False, transcriber=FakeTranscriber())) as client:
        await client.call_tool("list_messages", {"chat": JID, "transcribe_audio": True})
    reads = [argv for argv in calls() if "messages" in argv or "download" in argv]
    assert [argv[argv.index("--store") + 1] for argv in reads] == [os.path.realpath("/fake/stores/default")] * 2


def test_fitting_never_fails_where_a_smaller_limit_fit() -> None:
    """A transcript made whole loses its mark and flag, so a result can shrink as the cap grows.

    Fitting must still be monotonic: if a result fits under one limit, it fits under every larger one.
    """
    lengths = [8, 30, 31, 70, 75, 140, 400, 1200]

    def fitted(limit: int) -> bool:
        entries = [{"id": f"M{n}", "transcript": "ab cd " * (length // 6 + 1)} for n, length in enumerate(lengths)]
        payload = {"notice": "untrusted", "items": entries}
        fits = fit_transcripts(payload, entries, limit)
        assert fits == (len(to_json(payload)) <= limit)
        return fits

    outcomes = [fitted(limit) for limit in range(200, 2_400, 3)]
    first = outcomes.index(True)
    assert all(outcomes[first:]), [200 + 3 * n for n, fits in enumerate(outcomes) if n > first and not fits][:5]


async def test_result_too_large_without_transcripts_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WACLI_MCP_MAX_OUTPUT_CHARS", "1500")
    result = await get_media([f"VOICE{n}" for n in range(20)], transcriber=FakeTranscriber(), transcribe=True)
    assert result.is_error
    assert "fewer message_ids" in text_of(result)


def test_folder_names_never_meet() -> None:
    from wacli_mcp.media import _entry_name, _file_stem  # pyright: ignore[reportPrivateUsage]

    assert _entry_name("3EB0C5A1F2") == "3EB0C5A1F2"
    generated = _entry_name("3eb0c5a1f2")
    assert generated.startswith("~") and generated.lower() != "3eb0c5a1f2"
    # A plain uppercase id can't spell a generated name: plain names never contain `~`.
    assert _entry_name(generated.upper().lstrip("~")) == generated.upper().lstrip("~")
    assert _entry_name("CON").startswith("~") and _entry_name("lpt1").startswith("~")
    assert _file_stem("CON") == "_CON" and _file_stem("A.B") == "A_B"


async def test_failed_thread_start_releases_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    library = library_with(FakeTranscriber())
    media = await library.fetch(await library.resolve_store(), JID, "VOICE1")

    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", refuse)
        with pytest.raises(MediaError, match="could not start transcribing"):
            await library.transcribe(media)
    assert (await library.transcribe(media)).text == "words from VOICE1"


def test_fitting_keeps_short_transcripts_whole_when_that_fits() -> None:
    """Shortening a short transcript makes it longer (mark plus flag), so the search must not get stuck there.

    Nineteen 25-character transcripts and one long one: only caps that keep the short ones whole fit.
    """

    def entries() -> list[dict[str, Any]]:
        return [{"id": f"S{n}", "transcript": "x" * 25} for n in range(19)] + [{"id": "LONG", "transcript": "y" * 5000}]

    expected = entries()
    expected[-1].update(transcript="y" * 30 + " [...]", transcript_truncated=True)
    limit = len(to_json({"items": expected}))

    items = entries()
    payload = {"items": items}
    assert fit_transcripts(payload, items, limit)
    assert len(to_json(payload)) <= limit
    assert all(item["transcript"] == "x" * 25 for item in items[:19])


def test_store_paths_that_differ_only_in_case_stay_separate() -> None:
    """Windows folders can be case-sensitive, so `Team` and `team` may be two different stores."""
    library = library_with(FakeTranscriber())
    folder = library._folder  # pyright: ignore[reportPrivateUsage]
    (team, team_identity), (lower, lower_identity) = (
        folder(store, JID, "PHOTO1") for store in ("C:/Stores/Team", "C:/Stores/team")
    )
    assert team != lower
    assert team_identity["store_dir"] != lower_identity["store_dir"]


@pytest.mark.skipif(sys.platform == "win32", reason="creating symlinks needs extra privileges on Windows")
async def test_store_with_a_link_then_dotdot_matches_wacli(tmp_path: Path) -> None:
    """wacli drops `current/..` as text before following links, so `current/../store` is accounts/store."""
    (tmp_path / "archive" / "team").mkdir(parents=True)
    (tmp_path / "archive" / "store").mkdir()
    (tmp_path / "accounts" / "store").mkdir(parents=True)
    (tmp_path / "accounts" / "current").symlink_to(tmp_path / "archive" / "team", target_is_directory=True)
    settings = Settings(store=str(tmp_path / "accounts" / "current" / ".." / "store"))
    library = MediaLibrary(settings, Wacli(settings, command=(sys.executable, str(FAKE_WACLI))), FakeTranscriber())
    assert await library.resolve_store() == os.path.realpath(tmp_path / "accounts" / "store")
