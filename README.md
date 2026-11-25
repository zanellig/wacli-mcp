# wacli-mcp

[![CI](https://github.com/zanellig/wacli-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/zanellig/wacli-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wacli-mcp)](https://pypi.org/project/wacli-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/wacli-mcp)](https://pypi.org/project/wacli-mcp/)
[![License: MIT](https://img.shields.io/github/license/zanellig/wacli-mcp)](https://github.com/zanellig/wacli-mcp/blob/main/LICENSE)

An MCP server that lets AI agents such as Claude Code and Codex read your WhatsApp and send messages for you. It wraps [wacli](https://github.com/openclaw/wacli), an open-source WhatsApp client that links to your account as a device, the way WhatsApp Web does.

This is alpha software. Read the [changelog](https://github.com/zanellig/wacli-mcp/blob/main/CHANGELOG.md), the [release notes](https://github.com/zanellig/wacli-mcp/releases), and the [security policy](https://github.com/zanellig/wacli-mcp/security/policy) before you install it.

Ask your agent things like:

- "What did Ada say in the last hour?"
- "Find the message where someone sent the lease PDF."
- "What do Ada's voice notes from this morning say?"
- "Reply to the latest message from Ada: running 10 minutes late."
- "Send TEST to +15551234567."

> [!WARNING]
> wacli is not an official WhatsApp client. WhatsApp's help center says an account linked to an unofficial app can be blocked, temporarily or permanently. Use it on an account where you accept that risk. The people in your chats did not agree to having an AI read their messages, so decide whether you're comfortable with that.

## How it works

```
Claude Code / Codex  --stdio-->  wacli-mcp  --runs-->  wacli  --linked device-->  WhatsApp
```

- wacli keeps a local SQLite copy of your messages (in `~/.wacli` on Windows and macOS, `~/.local/state/wacli` on Linux; `wacli doctor` shows the active one). Reads come from that copy.
- Before a read, wacli-mcp runs a short `wacli sync --once` to pick up new messages, at most once a minute. The sync connects as your linked device and updates the local copy, like any linked device coming online: it can send delivery receipts (two grey ticks), never read receipts. If you run `wacli sync --follow` yourself, that process keeps the copy current and the per-read sync is skipped.
- `get_media` downloads photos, voice notes, videos and documents into a cache folder and returns their paths. It can transcribe voice notes and videos on your computer with [faster-whisper](https://github.com/SYSTRAN/faster-whisper); see [Attachments and voice notes](#attachments-and-voice-notes).
- `send_text` sends to a chat JID or a phone number. By default your agent and its client decide when to send, as with any other tool. You can make the server ask you itself before every send instead; see [Sending](#sending).
- The server talks to the agent over stdio only. It opens no network port.

## Requirements

- [wacli](https://github.com/openclaw/wacli) 0.26 or later, paired with your account.
- [uv](https://docs.astral.sh/uv/) (it can install Python for you), or [pipx](https://pipx.pypa.io/) with Python 3.11 or later.
- Optional, for transcription: the `transcribe` extra, about 2 GB of disk for the model and libraries, and ideally an NVIDIA GPU. See [Transcription](#transcription).
- Any MCP client. The optional server-side send confirmation needs a client that shows MCP elicitations, such as the Claude Code terminal app or the Codex CLI.

## Setup

### 1. Install and pair wacli

Install wacli from its [releases page](https://github.com/openclaw/wacli/releases/latest) (Windows, macOS, Linux) or with `brew install openclaw/tap/wacli`, and put it on your `PATH`. Then link it to WhatsApp:

```sh
wacli auth                         # shows a QR code: WhatsApp > Settings > Linked devices > Link a device
wacli auth --phone "+15551234567"  # or: prints a code to type in "Link with phone number instead"
```

The phone-number option works when your phone can't scan the screen, for example when WhatsApp runs in an Android emulator. Pairing syncs recent history (about three months).

### 2. Install the plugin

With wacli paired and `uvx` on your `PATH`, choose your client. The plugin registers the server and downloads wacli-mcp from PyPI on first use. Installing from GitHub requires access to this repository.

**Claude Code** (v2.1.292 or later)

```sh
claude plugin install wacli-mcp --marketplace zanellig/wacli-mcp
```

**Codex CLI**

```sh
codex plugin marketplace add zanellig/wacli-mcp && codex plugin add wacli-mcp@zanellig-wacli
```

Start a new session and ask your agent to run `whatsapp_status` to check the connection.

### Manual setup for MCP clients

If you prefer to register the server yourself, install the executable:

```sh
uv tool install wacli-mcp
```

With pipx, run `pipx install wacli-mcp`. If your shell can't find `wacli-mcp` after installation, run `uv tool update-shell` or `pipx ensurepath` for the installer you used, then restart your shell.

To run on demand, use `uvx wacli-mcp` as your agent's stdio command. For the setup commands below, replace `-- wacli-mcp` with `-- uvx wacli-mcp`.

Then register the executable with your client.

**Claude Code**, for every project:

```sh
claude mcp add --scope user whatsapp -- wacli-mcp
```

**Codex CLI**:

```sh
codex mcp add whatsapp -- wacli-mcp
```

Codex runs the read tools without asking because they are marked read-only, and asks you before each `send_text` call because it is marked as changing something. To let Codex send without asking, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.whatsapp.tools.send_text]
approval_mode = "approve"
```

Codex starts MCP servers with a filtered environment. To pass a `WACLI_MCP_*` setting, put it in the server's `env` table, for example `env = { WACLI_MCP_SEND_CONFIRMATION = "server" }`. In Claude Code, use `claude mcp add --env WACLI_MCP_SEND_CONFIRMATION=server --scope user whatsapp -- wacli-mcp`.

**Other MCP clients**: run `wacli-mcp` as a stdio server.

## Tools

| Tool | What it does |
| --- | --- |
| `whatsapp_status` | Checks that wacli is installed, paired, and has a readable store. |
| `list_chats` | Chats by recent activity, with JIDs and unread counts. Filters: `query`, `unread_only`. |
| `list_messages` | A chat's messages, newest first. Filters: `before`, `after`, `from_them_only`. `transcribe_audio` adds voice-note transcripts. |
| `search_messages` | Full-text search, optionally within one chat or from one sender. |
| `get_message_context` | The messages around one message. |
| `search_contacts` | Contacts by name or number, with their JIDs. |
| `get_media` | Downloads the attachments of up to 20 messages and returns their paths. Shows photos inline, and with `transcribe` returns the text of voice notes and videos. |
| `send_text` | Sends a text message, optionally as a reply, to a chat JID or a phone number. |

All tools except `send_text` call wacli with `--read-only`. Results label message content, attachments and transcripts as untrusted: things other people wrote or recorded, not instructions.

## Attachments and voice notes

Messages with an attachment have a `media_type` (`image`, `audio`, `video`, `sticker`, `document`). `get_media` takes the chat's JID and up to 20 message `id`s. For each message it:

- downloads the file with `wacli media download --read-only` into `<media folder>/store-<id>/<chat>/<message id>/`, with a separate `store-<id>` folder for each wacli store, so each WhatsApp account has its own. The file is named after the message id, with an extension that matches its type, such as `.jpg`, `.ogg`, `.mp4` or `.pdf`. Your agent opens it from there with its own file tools.
- also returns JPEG and PNG photos up to 1 MB inline, so the model sees them without opening a file. One result shows at most 4 MB of photos inline; the rest are listed by path.
- with `transcribe: true`, adds the text of voice notes, audio and video.

A file that can't be downloaded or saved gets an `error` on its own item and doesn't stop the others. Files and transcripts are saved, so asking for the same message again returns at once. A saved copy is reused only for the same chat, message and wacli store.

Each transcript comes with `transcript_file`, the saved JSON that holds its full text. When transcripts would make a result longer than `WACLI_MCP_MAX_OUTPUT_CHARS`, the longest ones are shortened and marked `transcript_truncated`.

`list_messages` with `transcribe_audio: true` does the same for the voice notes it returns, up to the newest 20, so "read my chat with Ada, voice notes included" is one call. Without the transcription extra, it still returns transcripts saved earlier and downloads nothing.

The media folder is `wacli-mcp/media` in your user cache folder: `%LOCALAPPDATA%` on Windows, `~/Library/Caches` on macOS, `~/.cache` on Linux. `whatsapp_status` shows the exact path. Delete the folder to remove everything downloaded; wacli-mcp downloads files again when asked.

WhatsApp keeps attachments on its servers for a limited time. For an older file, the error says so, and `wacli media download --retry-on-phone` can ask your phone to upload it again.

### Transcription

Transcription runs [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your computer, so audio never leaves it. It is an optional extra, because it adds about 2 GB of libraries and model files:

```sh
uv tool install --python 3.13 "wacli-mcp[transcribe]"        # CPU, or an NVIDIA GPU with CUDA 12 and cuDNN 9 installed
uv tool install --python 3.13 "wacli-mcp[transcribe-cuda]"   # Windows with an NVIDIA GPU: installs the CUDA libraries too
```

`--python 3.13` is there because onnxruntime, which faster-whisper needs, publishes wheels only up to Python 3.14. Without it, uv may pick a newer Python and fail to install. With `uvx`, use `uvx --python 3.13 --from "wacli-mcp[transcribe]" wacli-mcp` as the server command. Without the extra, `get_media` still returns the files, with a note on how to turn transcription on.

If `uv tool install` fails with "Access is denied" on Windows, a running agent session is still using wacli-mcp. Close those sessions and run it again.

- The default model is `large-v3-turbo`. The first transcription downloads it, about 1.6 GB, into the Hugging Face cache.
- On an NVIDIA GPU, a minute of audio takes a few seconds. A CPU takes several times longer. With `WACLI_MCP_TRANSCRIBE_DEVICE=auto`, wacli-mcp tries the GPU and falls back to the CPU if CUDA doesn't work. `whatsapp_status` shows the device in use, and the GPU error if there was one.
- The model stays loaded until the server stops, holding about 2 GB of GPU or system memory.
- Whisper detects each file's language. Set `WACLI_MCP_TRANSCRIBE_LANGUAGE`, for example to `es`, if it guesses wrong on short notes.
- Recordings longer than an hour are not transcribed, to bound the memory the server uses.
- One file is transcribed at a time, and up to 8 files can wait their turn. A transcription finishes and saves its text even if the client stops waiting, and asking again for the same file joins it instead of starting another.
- Transcripts are machine output: names and rare words can come out wrong.

Codex stops a tool call after 60 seconds by default, and a first transcription can take longer while the model downloads. The transcription keeps running and saves its result, so asking again returns it. To avoid the timeout, raise the limit in `~/.codex/config.toml`:

```toml
[mcp_servers.whatsapp]
tool_timeout_sec = 600
```

## Sending

`send_text` takes `to`, which is a chat JID (`15551234567@s.whatsapp.net`, `120363...@g.us`) or a phone number in international format (`+54 9 11 1234-5678`). It refuses contact names, because a name can match more than one chat; the agent looks names up with `search_contacts` or `list_chats` first. The server tells the agent to send right away when you gave the recipient and the exact text, and to show you any text it wrote itself before sending.

For a phone number, wacli asks WhatsApp for the account's current identifier just before sending, and WhatsApp can return the same account's other identifier (a `@lid` JID). When that happens, the result says where the message went.

### Who confirms a send

`WACLI_MCP_SEND_CONFIRMATION` chooses who confirms a send:

- **`client`** (default): the agent and its client, like any other tool. If your client asks before tool calls, you approve the send there; in a mode that skips approvals, the agent sends as soon as it decides to. Use this with clients that don't show MCP elicitations, such as the Claude Agent SDK inside other apps.
- **`server`**: wacli-mcp asks you itself, through an [MCP elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation), and sends only after an explicit yes. Your client's permission mode can't skip it, but a client that doesn't show elicitations can't send at all.

In `server` mode the prompt looks like this:

```
Send this WhatsApp message?

Recipient JID: 15551234567@s.whatsapp.net
Chat name: Ada
Reply to message ID: 3EB0C5A1F2
Text: 31 characters on 2 line(s). Each line below starts with "| ".
| Running 10 minutes late.
| Sorry!
```

- The recipient JID always comes first, on its own line. The chat name comes from your local store and other people can influence it, so it is shown on one line with any line breaks marked.
- Each line of the message starts with `| `, so message text can't imitate the fields above it.
- Invisible characters, such as zero-width spaces, text-direction overrides, and line separators, appear as `<U+XXXX>`. A literal `<U+` in the text appears as `<U+003C>U+`, so every marker you see stands for a real hidden character.
- Only an explicit `true` in the form counts as yes.
- One confirmation sends one message. If the client resubmits an answered confirmation, for example after losing the result, nothing is sent again.

The prompt reaches you through your MCP client, so it protects you only as far as you trust that client to show it to you and report your answer. What happens in `server` mode with the clients checked so far:

| Client and mode | Result |
| --- | --- |
| Codex TUI with `approval_policy = "on-request"` | Shows the confirmation form. |
| Codex with `--dangerously-bypass-approvals-and-sandbox`, or `codex exec` | Not sent. |
| Claude Code, `-p` (headless), including `--permission-mode bypassPermissions` | Not sent. |
| Claude Agent SDK hosts that don't show elicitations | Not sent. |
| A client without elicitation support | Not sent; the tool returns an error. |

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WACLI_MCP_SEND_CONFIRMATION` | `client` | Who confirms a send: `client` or `server`. See [Who confirms a send](#who-confirms-a-send). |
| `WACLI_MCP_READ_ONLY` | `false` | Hide `send_text` and skip the per-read sync, so no tool connects to WhatsApp as your linked device or writes wacli's local copy. `get_media` still downloads attachments from WhatsApp's media servers. Run `wacli sync --follow` separately to keep reads current. |
| `WACLI_MCP_WACLI_PATH` | `wacli` | Path to the wacli executable. |
| `WACLI_MCP_ACCOUNT` | unset | wacli named account (`--account`). |
| `WACLI_MCP_STORE` | unset | wacli store directory (`--store`). |
| `WACLI_MCP_LINK_PREVIEWS` | `false` | Let wacli fetch link previews for URLs in sent messages. |
| `WACLI_MCP_SYNC_BEFORE_READ` | `true` | Run `wacli sync --once` before reads when the store may be stale. |
| `WACLI_MCP_SYNC_MAX_AGE_SECONDS` | `60` | Minimum time between those syncs. |
| `WACLI_MCP_SYNC_IDLE_EXIT_SECONDS` | `4` | How long a sync waits for new events before it stops. |
| `WACLI_MCP_SYNC_TIMEOUT_SECONDS` | `45` | Upper bound for one sync. |
| `WACLI_MCP_COMMAND_TIMEOUT_SECONDS` | `60` | Upper bound for any other wacli command. wacli gets a slightly shorter `--timeout`, so a send queued behind `wacli sync --follow` can't go out after wacli-mcp has reported a timeout. |
| `WACLI_MCP_MAX_OUTPUT_CHARS` | `60000` | Truncate longer tool results. |
| `WACLI_MCP_MEDIA_DIR` | user cache folder | Where `get_media` saves attachments and transcripts. See [Attachments and voice notes](#attachments-and-voice-notes). |
| `WACLI_MCP_INLINE_IMAGE_MAX_BYTES` | `1000000` | Largest photo that `get_media` also returns inline. `0` turns inline photos off. |
| `WACLI_MCP_TRANSCRIBE_MODEL` | `large-v3-turbo` | faster-whisper model name, such as `small` for slower CPUs, or a local model folder. |
| `WACLI_MCP_TRANSCRIBE_DEVICE` | `auto` | `auto`, `cuda` or `cpu`. `auto` uses an NVIDIA GPU when it works. |
| `WACLI_MCP_TRANSCRIBE_LANGUAGE` | unset | Language code for every file, such as `es`. Unset: detected per file. |

## Security

An agent with this server has your private messages, reads text that anyone can send you, and can send messages. Simon Willison calls that combination [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/): a crafted incoming message can try to make the agent leak data. Invariant Labs [demonstrated it](https://invariantlabs.ai/blog/whatsapp-mcp-exploited) against an earlier WhatsApp MCP server, where an incoming message made the agent send the user's contacts to the attacker.

What wacli-mcp does about it:

- Results label message content as untrusted, and the server's instructions tell the agent that a message asking it to send or reveal something is content to report, not a request from you.
- In `server` confirmation mode, every send needs your answer to a prompt the server requests itself, showing the real recipient and the full text with hidden characters made visible. Each answer sends at most one message.
- Read tools run wacli with `--read-only`. `get_media` saves files only inside its media folder, and checks that wacli wrote the file where it was told to.
- Transcription runs on your computer. Voice notes are not sent to a transcription service.
- No network listener, and link previews are off by default.

What it does not protect against:

- In the default `client` mode, an agent that follows instructions hidden in a message can send messages, limited only by your client's tool approvals. Use `server` mode, or keep tool approvals on, if that matters to you.
- A client that answers the `server` prompt without asking you. The server trusts the MCP client to show the prompt to a person.
- An agent that can run arbitrary shell commands can run `wacli send` directly, or read the wacli store, without going through this server.
- Anything the agent reads goes to the model provider that runs the agent, including photos shown inline and transcripts.
- A photo, document or voice note can carry instructions too, as text in an image or words spoken in a note. These get the same untrusted label as message text, and the model decides what to do with them.
- Downloaded attachments stay in the media folder, unencrypted, until you delete them. On macOS and Linux, wacli-mcp creates the folder readable only by your user.
- The store's `session.db` holds your linked-device keys. Anyone who copies it can act as that device. To revoke it, open WhatsApp on your phone, go to **Settings > Linked devices**, and log the device out.

See the [security policy](https://github.com/zanellig/wacli-mcp/security/policy) to report a vulnerability. Fixes are listed in the [changelog](https://github.com/zanellig/wacli-mcp/blob/main/CHANGELOG.md).

## Development

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync basedpyright
uv run --no-sync pytest
```

The tests run the server in-process against `tests/fake_wacli.py`, so they never touch a real WhatsApp account.

## License

[MIT](LICENSE). wacli-mcp is not affiliated with WhatsApp, Meta, or the wacli project.

<!-- mcp-name: io.github.zanellig/wacli-mcp -->
