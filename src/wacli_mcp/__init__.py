"""MCP server that gives AI agents your WhatsApp through the wacli command-line client."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wacli-mcp")
except PackageNotFoundError:  # running from a source tree without installed metadata
    __version__ = "0.0.0"


def main() -> None:
    """Run the server over stdio."""
    from wacli_mcp.server import build_server

    build_server().run("stdio")
