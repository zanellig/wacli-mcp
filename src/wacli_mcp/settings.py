"""Configuration, read from `WACLI_MCP_*` environment variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server settings. Every field maps to `WACLI_MCP_<FIELD_NAME>`."""

    model_config = SettingsConfigDict(env_prefix="WACLI_MCP_", extra="ignore")

    wacli_path: str = Field(
        default="wacli",
        description="Path to the wacli executable, or a name found on PATH.",
    )
    account: str | None = Field(
        default=None,
        description="wacli named account (passed as --account).",
    )
    store: str | None = Field(
        default=None,
        description="wacli store directory (passed as --store).",
    )
    read_only: bool = Field(
        default=False,
        description=(
            "Hide the send tool and skip the pre-read sync, so no tool connects to WhatsApp as your linked "
            "device or writes wacli's local store. get_media still downloads attachments from WhatsApp's media "
            "servers. Run `wacli sync --follow` separately to keep reads current."
        ),
    )
    send_confirmation: Literal["client", "server"] = Field(
        default="client",
        description=(
            "Who confirms a send. `client`: the agent and its client's tool approval, like any other "
            "tool. `server`: wacli-mcp asks you itself through an MCP elicitation and refuses to send "
            "when the client can't show it."
        ),
    )
    link_previews: bool = Field(
        default=False,
        description="Let wacli fetch link previews for URLs in sent messages.",
    )
    sync_before_read: bool = Field(
        default=True,
        description=(
            "Run a short `wacli sync --once` before reads when the local store may be stale. "
            "Skipped when a `wacli sync --follow` process already owns the store."
        ),
    )
    sync_max_age_seconds: float = Field(
        default=60,
        ge=0,
        description="Skip the pre-read sync if the last one finished this recently.",
    )
    sync_idle_exit_seconds: float = Field(
        default=4,
        gt=0,
        description="How long the pre-read sync waits for new events before exiting.",
    )
    sync_timeout_seconds: float = Field(
        default=45,
        gt=0,
        description="Upper bound for one pre-read sync.",
    )
    command_timeout_seconds: float = Field(
        default=60,
        gt=0,
        description="Upper bound for one wacli command.",
    )
    max_output_chars: int = Field(
        default=60_000,
        gt=1_000,
        description="Truncate tool results longer than this.",
    )
    media_dir: Path | None = Field(
        default=None,
        description=(
            "Where get_media saves attachments and transcripts. Default: `wacli-mcp/media` in the user cache "
            "directory (`%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, `~/.cache` on Linux)."
        ),
    )
    inline_image_max_bytes: int = Field(
        default=1_000_000,
        ge=0,
        description="get_media also returns images up to this size inline, so the model sees them. 0 turns it off.",
    )
    transcribe_model: str = Field(
        default="large-v3-turbo",
        min_length=1,
        description="faster-whisper model name or local model directory used for transcription.",
    )
    transcribe_device: Literal["auto", "cuda", "cpu"] = Field(
        default="auto",
        description="`auto` uses an NVIDIA GPU when one works and falls back to the CPU.",
    )
    transcribe_shared: bool = Field(
        default=True,
        description=(
            "Transcribe in one background service that every wacli-mcp session shares, so the model is "
            "loaded once per machine instead of once per agent session. `false` loads it in each session."
        ),
    )
    transcribe_idle_minutes: float = Field(
        default=10,
        gt=0,
        description="The shared transcription service exits after this long without work, freeing GPU memory.",
    )
    transcribe_language: str | None = Field(
        default=None,
        description="Language code such as `es` or `en`. Unset: Whisper detects the language of each file.",
    )

    @property
    def media_root(self) -> Path:
        """The media folder as an absolute path: `media_dir`, or the default in the user cache directory.

        Both the attachment cache and the shared transcription service, which keeps its files in
        `.transcriber` inside it, find their folder here.
        """
        return (self.media_dir or default_media_dir()).expanduser().resolve()


def default_media_dir() -> Path:
    """The per-user cache directory: `%LOCALAPPDATA%`, `~/Library/Caches`, or `$XDG_CACHE_HOME`."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "wacli-mcp" / "media"
