"""Helpers shared by the test modules: a server wired to the fake wacli, and a fake transcriber."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from wacli_mcp.settings import Settings
from wacli_mcp.transcription import Transcriber, Transcript
from wacli_mcp.wacli import Wacli

FAKE_WACLI = Path(__file__).with_name("fake_wacli.py")
JID = "15551234567@s.whatsapp.net"
Calls = Callable[[], list[list[str]]]


def make_server(
    *, max_stdout_bytes: int | None = None, transcriber: Transcriber | None = None, **overrides: Any
) -> MCPServer:
    from wacli_mcp.server import build_server

    settings = Settings(**overrides)
    wacli = Wacli(settings, command=(sys.executable, str(FAKE_WACLI)))
    if max_stdout_bytes is not None:
        wacli.max_stdout_bytes = max_stdout_bytes
    return build_server(settings, wacli, transcriber)


def text_of(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@dataclass
class FakeTranscriber:
    """Records which files it was asked to transcribe. `fail` makes it raise for every file."""

    reason: str | None = None
    fail: str | None = None
    calls: list[Path] = field(default_factory=list)
    cache_tag: str = "fake-auto"

    def unavailable_reason(self) -> str | None:
        return self.reason

    def status(self) -> dict[str, Any]:
        return {"available": self.reason is None}

    def transcribe(self, path: Path) -> Transcript:
        self.calls.append(path)
        if self.fail:
            raise RuntimeError(self.fail)
        return Transcript(f"words from {path.stem}", "es", 12.5, "fake", "cpu")
