"""MCP server that gives AI agents your WhatsApp through the wacli command-line client."""

from __future__ import annotations

from wacli_mcp._version import __version__

__all__ = ["__version__", "main"]


def main() -> None:
    """Run the server over stdio."""
    from wacli_mcp.server import build_server

    build_server().run("stdio")
