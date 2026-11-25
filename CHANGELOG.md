# Changelog

All notable changes to wacli-mcp are listed here, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is below 1.0, a minor release can change behavior; each change below says so when it does.

Every release has a page with these notes on the [releases page](https://github.com/zanellig/wacli-mcp/releases).

## [Unreleased]

## [0.1.1] - 2026-11-25

### Added

- An npm package, so `npx -y wacli-mcp` runs the server. It starts the PyPI release with the same version through uv. If uv is not on `PATH`, it downloads a pinned uv release from GitHub, checks its SHA-256, and caches it. `WACLI_MCP_EXTRAS` picks extras such as `transcribe`.
- The MCP Registry entry lists the npm package next to the PyPI one.

## [0.1.0] - 2026-11-25

First release. It is alpha software: it works with real accounts, and its interface can still change before 1.0. It needs [wacli](https://github.com/openclaw/wacli) 0.26 or later and Python 3.11 or later.

wacli is not an official WhatsApp client, and WhatsApp can block an account linked to an unofficial app. Read the [security section of the README](https://github.com/zanellig/wacli-mcp#security) for what this server does and does not protect against before you give an agent access to your messages.

### Added

- Eight MCP tools over stdio: `whatsapp_status`, `list_chats`, `list_messages`, `search_messages`, `get_message_context`, `search_contacts`, `get_media`, and `send_text`. The server opens no network port.
- Read tools run wacli with `--read-only`, and their results label message content, attachments and transcripts as untrusted text written by other people.
- A short `wacli sync --once` before reads, at most once a minute, so results are current. It can send delivery receipts but never read receipts.
- `send_text` sends to a chat JID or an international phone number and refuses contact names, which can match more than one chat.
- `WACLI_MCP_SEND_CONFIRMATION`: `client` (default) leaves the decision to the agent and its client, as with any tool. `server` makes the server ask you itself through an MCP elicitation, and it sends only after an explicit yes.
- Server confirmation prompt that shows the real recipient JID first and the full text with hidden characters made visible. One confirmation sends at most one message, even if the client resubmits it.
- `get_media` downloads the attachments of up to 20 messages (photos, voice notes, videos, stickers and documents) and returns their local paths. It also returns JPEG and PNG photos up to 1 MB each, and 4 MB in total, inline, so the model sees them directly. Files are saved in a cache folder, named by message id with an extension that matches their type, and reused on later calls for the same chat, message and wacli account.
- Local transcription with [faster-whisper](https://github.com/SYSTRAN/faster-whisper): `get_media` with `transcribe: true` returns the text of voice notes, audio and video up to an hour long, and `list_messages` with `transcribe_audio: true` adds transcripts to the newest 20 voice notes it returns. Audio stays on your computer. Install the `transcribe` extra, or `transcribe-cuda` for an NVIDIA GPU on Windows. Without it, both tools still return files and transcripts saved earlier, and say how to enable transcription.
- Long transcripts are shortened to keep a result under `WACLI_MCP_MAX_OUTPUT_CHARS`, with a path to the full text. A repeated request waits for a transcription that is still running instead of starting a second one.
- Agent sessions share one transcription service instead of each loading Whisper, so the model takes GPU memory once per machine. The first session that transcribes starts it in the background; it talks to sessions over a local, key-authenticated pipe or socket and exits after `WACLI_MCP_TRANSCRIBE_IDLE_MINUTES` without work. A file several sessions ask for at once is transcribed once. `WACLI_MCP_TRANSCRIBE_SHARED=false` keeps transcription in each session.
- Settings for the media folder, inline photo size, and the transcription model, device and language. `whatsapp_status` reports the media folder and whether transcription is available.
- `WACLI_MCP_READ_ONLY` hides `send_text` and skips the pre-read sync, so no tool connects to WhatsApp or writes wacli's local copy.
- Settings for the wacli path, account, store, link previews (off by default), sync timing, command timeouts, and output size. See the [configuration table](https://github.com/zanellig/wacli-mcp#configuration).
- Installation from PyPI with `uv tool install wacli-mcp` or `uvx wacli-mcp`, an [MCP Registry](https://registry.modelcontextprotocol.io/) entry named `io.github.zanellig/wacli-mcp`, and plugins for Claude Code and Codex.
- A [security policy](https://github.com/zanellig/wacli-mcp/security/policy) that lists what is in and out of scope for a report.

[Unreleased]: https://github.com/zanellig/wacli-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/zanellig/wacli-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/zanellig/wacli-mcp/releases/tag/v0.1.0
