from __future__ import annotations

import json
from pathlib import Path

import pytest

from support import Calls


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def media_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every test's downloads out of the real user cache directory."""
    path = tmp_path / "media"
    monkeypatch.setenv("WACLI_MCP_MEDIA_DIR", str(path))
    return path


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Calls:
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_WACLI_LOG", str(log))

    def read() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return read
