"""Runs the real shared transcription service with a fake model instead of Whisper.

Accepts the same `--dir` and `--idle-exit` arguments as `python -m wacli_mcp.transcription_service`.
Appends one line per event to `<dir>/events.log`: `load <model>` when a model is created, and
`transcribe <file name>` for each transcription.

Environment:
    FAKE_SERVICE_DELAY: seconds each transcription takes.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from wacli_mcp.transcription import Transcript
from wacli_mcp.transcription_service import serve

_log_lock = threading.Lock()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--idle-exit", type=float, default=600)
    args = parser.parse_args()
    events = args.dir / "events.log"

    def record(line: str) -> None:
        with _log_lock, open(events, "a", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {line}\n")

    class FakeModel:
        def __init__(self, model: str, device: str) -> None:
            self.model, self.device = model, device
            record(f"load {model}")

        def transcribe_in(self, path: Path, language: str | None) -> Transcript:
            record(f"transcribe {path.name}")
            time.sleep(float(os.environ.get("FAKE_SERVICE_DELAY", "0")))
            return Transcript(f"shared words from {path.stem}", language or "es", 1.0, self.model, "fake")

        def status(self) -> dict[str, Any]:
            return {"device": "fake"}

    return serve(args.dir, args.idle_exit, FakeModel)


if __name__ == "__main__":
    sys.exit(main())
