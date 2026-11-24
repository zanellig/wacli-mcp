# Changelog

All notable changes to wacli-mcp are listed here, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below 1.0, a minor release can change behavior; each change below says so when it does.

Every release has a page with these notes on the [releases page](https://github.com/zanellig/wacli-mcp/releases).

## [Unreleased]

### Added

- `get_media` downloads the attachments of up to 20 messages (photos, voice notes, videos, stickers and documents) and returns their local paths. It also returns JPEG and PNG photos up to 1 MB inline, so the model sees them directly. Files are saved in a cache folder, named by message id with an extension that matches their type, and reused on later calls.
- Local transcription with [faster-whisper](https://github.com/SYSTRAN/faster-whisper): `get_media` with `transcribe: true` returns the text of voice notes, audio and video, and `list_messages` with `transcribe_audio: true` adds transcripts to the newest 20 voice notes it returns. Audio stays on your computer. Install the `transcribe` extra, or `transcribe-cuda` for an NVIDIA GPU on Windows. Without it, both tools still work and say how to enable transcription.
- Settings for the media folder, inline photo size, and the transcription model, device and language. `whatsapp_status` reports the media folder and whether transcription is available.

### Changed

- Results now label attachments and transcripts as untrusted, like message text.

## [0.1.0] - 2026-11-24

First release. It is alpha software: it works with real accounts, and its interface can still change before 1.0. It needs [wacli](https://github.com/openclaw/wacli) 0.26 or later and Python 3.11 or later.

wacli is not an official WhatsApp client, and WhatsApp can block an account linked to an unofficial app. Read the [security section of the README](https://github.com/zanellig/wacli-mcp#security) for what this server does and does not protect against before you give an agent access to your messages.

### Added

- Seven MCP tools over stdio: `whatsapp_status`, `list_chats`, `list_messages`, `search_messages`, `get_message_context`, `search_contacts`, and `send_text`. The server opens no network port.
- Read tools run wacli with `--read-only`, and their results label message content as untrusted text written by other people.
- A short `wacli sync --once` before reads, at most once a minute, so results are current. It can send delivery receipts but never read receipts.
- `send_text` sends to a chat JID or an international phone number and refuses contact names, which can match more than one chat.
- `WACLI_MCP_SEND_CONFIRMATION`: `client` (default) leaves the decision to the agent and its client, as with any tool. `server` makes the server ask you itself through an MCP elicitation, and it sends only after an explicit yes.
- Server confirmation prompt that shows the real recipient JID first and the full text with hidden characters made visible. One confirmation sends at most one message, even if the client resubmits it.
- `WACLI_MCP_READ_ONLY` hides `send_text` and skips the pre-read sync, so no tool connects to WhatsApp or writes wacli's local copy.
- Settings for the wacli path, account, store, link previews (off by default), sync timing, command timeouts, and output size. See the [configuration table](https://github.com/zanellig/wacli-mcp#configuration).
- Installation from PyPI with `uv tool install wacli-mcp` or `uvx wacli-mcp`, an [MCP Registry](https://registry.modelcontextprotocol.io/) entry named `io.github.zanellig/wacli-mcp`, and plugins for Claude Code and Codex.
- A [security policy](https://github.com/zanellig/wacli-mcp/security/policy) that lists what is in and out of scope for a report.

[Unreleased]: https://github.com/zanellig/wacli-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zanellig/wacli-mcp/releases/tag/v0.1.0
