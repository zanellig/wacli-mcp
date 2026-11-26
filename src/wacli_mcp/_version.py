"""The installed package version, in a module that imports nothing else from wacli_mcp."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wacli-mcp")
except PackageNotFoundError:  # running from a source tree without installed metadata
    __version__ = "0.0.0"
