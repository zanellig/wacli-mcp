"""The shared transcription service, run as a real background process with a fake model."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any

import pytest

from wacli_mcp.settings import Settings
from wacli_mcp.transcription import Transcript
from wacli_mcp.transcription_service import PROTOCOL, ServiceAddress, SharedTranscriber

from support import FakeTranscriber

FAKE_SERVICE = Path(__file__).with_name("fake_transcription_service.py")


@pytest.fixture
def sessions(media_dir: Path) -> Iterator[list[SharedTranscriber]]:
    """Session clients created by a test. The service they share is stopped afterwards."""
    created: list[SharedTranscriber] = []
    yield created
    if created:
        created[0].stop_service()


def session(sessions: list[SharedTranscriber], **overrides: Any) -> SharedTranscriber:
    client = SharedTranscriber(
        Settings(**overrides), service_command=[sys.executable, str(FAKE_SERVICE)], start_timeout=20
    )
    sessions.append(client)
    return client


def audio(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.ogg"
    path.write_bytes(b"OggS fake")
    return path


def events(media_dir: Path) -> list[str]:
    log = media_dir / ".transcriber" / "events.log"
    if not log.exists():
        return []
    return [line.split(" ", 1)[1] for line in log.read_text(encoding="utf-8").splitlines()]


def service_pid(client: SharedTranscriber) -> int | None:
    service = client.status()["service"]
    return service["pid"] if isinstance(service, dict) else None


def test_sessions_share_one_service_and_one_model(
    sessions: list[SharedTranscriber], media_dir: Path, tmp_path: Path
) -> None:
    first, second = session(sessions), session(sessions)
    assert first.transcribe(audio(tmp_path, "one")).text == "shared words from one"
    assert second.transcribe(audio(tmp_path, "two")).text == "shared words from two"
    assert [e for e in events(media_dir) if e.startswith("load")] == ["load large-v3-turbo"]
    assert service_pid(first) == service_pid(second) is not None


def test_a_file_asked_for_by_two_sessions_is_transcribed_once(
    sessions: list[SharedTranscriber], media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SERVICE_DELAY", "1.5")
    path = audio(tmp_path, "same")
    session(sessions).transcribe(audio(tmp_path, "warm-up"))  # start the service before racing
    results: list[Transcript] = []
    threads = [
        threading.Thread(target=lambda client=client: results.append(client.transcribe(path)))
        for client in (session(sessions), session(sessions))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert [r.text for r in results] == ["shared words from same"] * 2
    assert events(media_dir).count("transcribe same.ogg") == 1


def test_an_idle_service_exits_and_the_next_request_restarts_it(
    sessions: list[SharedTranscriber], media_dir: Path, tmp_path: Path
) -> None:
    client = session(sessions, transcribe_idle_minutes=0.02)  # 1.2 s
    client.transcribe(audio(tmp_path, "one"))
    first_pid = service_pid(client)
    deadline = time.monotonic() + 20
    while service_pid(client) is not None and time.monotonic() < deadline:
        time.sleep(0.3)
    assert service_pid(client) is None, "the service should exit when idle, freeing the model"
    client.transcribe(audio(tmp_path, "two"))
    assert service_pid(client) not in {None, first_pid}
    assert [e for e in events(media_dir) if e.startswith("load")] == ["load large-v3-turbo"] * 2


def test_a_session_falls_back_to_its_own_model_when_the_service_cannot_start(tmp_path: Path) -> None:
    fallback = FakeTranscriber(text="local words")
    client = SharedTranscriber(
        Settings(),
        service_command=[sys.executable, "-c", "raise SystemExit(3)"],
        fallback=fallback,
        start_timeout=1,
    )
    assert client.transcribe(audio(tmp_path, "one")).text == "local words"
    status = client.status()
    assert "did not start" in status["service_error"]
    assert status["service"].startswith("not running")


def test_only_holders_of_the_key_can_talk_to_the_service(sessions: list[SharedTranscriber], tmp_path: Path) -> None:
    client = session(sessions)
    client.transcribe(audio(tmp_path, "one"))
    where = ServiceAddress.for_folder(client._where.folder)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises((AuthenticationError, EOFError, ConnectionError)):
        Client(where.address, family=where.family, authkey=b"not the key")

    with Client(where.address, family=where.family, authkey=where.key()) as connection:
        connection.send_bytes(json.dumps({"protocol": PROTOCOL + 1, "op": "status"}).encode())
        response = json.loads(connection.recv_bytes())
    assert response["ok"] is False and "protocol" in response["error"]


def test_the_server_shares_transcription_by_default(media_dir: Path) -> None:
    from support import make_server

    for shared in (True, False):
        server = make_server(transcribe_shared=shared)
        assert server is not None
    assert isinstance(SharedTranscriber(Settings()).status()["service"], str)
