"""Configuration, read from `WACLI_MCP_*` environment variables."""

from __future__ import annotations

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
            "Hide the send tool and skip the pre-read sync, so no tool connects to WhatsApp "
            "or writes the local store. Run `wacli sync --follow` separately to keep reads current."
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
