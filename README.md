# wacli-mcp

An MCP server that lets AI agents such as Claude Code and Codex read your WhatsApp and send replies, with your confirmation on every send. It wraps [wacli](https://github.com/openclaw/wacli), an open-source WhatsApp client that links to your account as a device, the way WhatsApp Web does.

Ask your agent things like:

- "What did Ada say in the last hour?"
- "Find the message where someone sent the lease PDF."
- "Reply to the latest message from Ada: running 10 minutes late."

> [!WARNING]
> wacli is not an official WhatsApp client. WhatsApp's help center says an account linked to an unofficial app can be blocked, temporarily or permanently. Use it on an account where you accept that risk. The people in your chats did not agree to having an AI read their messages, so decide whether you're comfortable with that.

## How it works

```
Claude Code / Codex  --stdio-->  wacli-mcp  --runs-->  wacli  --linked device-->  WhatsApp
                                    |
                                    +--> confirmation prompt shown to you before each send
```

- wacli keeps a local SQLite copy of your messages in `~/.wacli`. Reads come from that copy.
- Before a read, wacli-mcp runs a short `wacli sync --once` to pick up new messages, at most once a minute. If you run `wacli sync --follow` yourself, the store is always current and this step is skipped.
- `send_text` asks you to confirm the recipient and the exact text through an [MCP elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation) before it calls `wacli send`. If you decline, don't answer, or the client can't show the prompt, nothing is sent.
- The server talks to the agent over stdio only. It opens no network port.

## Requirements

- [wacli](https://github.com/openclaw/wacli) 0.26 or later, paired with your account.
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.11+ for you).
- An MCP client that supports elicitation for sending. Claude Code and Codex CLI do. Reading works in any MCP client.

## Setup

### 1. Install and pair wacli

Install wacli from its [releases page](https://github.com/openclaw/wacli/releases/latest) (Windows, macOS, Linux) or with `brew install openclaw/tap/wacli`, and put it on your `PATH`. Then link it to WhatsApp:

```sh
wacli auth                         # shows a QR code: WhatsApp > Settings > Linked devices > Link a device
wacli auth --phone "+15551234567"  # or: prints a code to type in "Link with phone number instead"
```

The phone-number option works when your phone can't scan the screen, for example when WhatsApp runs in an Android emulator. Pairing syncs recent history (about three months).

### 2. Install wacli-mcp

```sh
uv tool install git+https://github.com/zanellig/wacli-mcp
```

From a local clone, use `uv tool install .` instead. Either way, `wacli-mcp` ends up on your `PATH`.

### 3. Add it to your agent

**Claude Code**, for every project:

```sh
claude mcp add --scope user whatsapp -- wacli-mcp
```

Claude Code asks before each MCP tool call. The server already asks before every send, so you can let the tools run without the extra prompt by adding this to `~/.claude/settings.json`:

```json
{ "permissions": { "allow": ["mcp__whatsapp__*"] } }
```

**Codex CLI**:

```sh
codex mcp add whatsapp -- wacli-mcp
```

Codex skips its own approval for the read tools because they are marked read-only. For `send_text`, it would ask once itself and then the server would ask again. To keep only the server's prompt, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.whatsapp.tools.send_text]
approval_mode = "approve"   # wacli-mcp shows its own confirmation before sending
```

Codex starts MCP servers with a filtered environment. To pass a `WACLI_MCP_*` setting, put it in the server's `env` table, for example `env = { WACLI_MCP_ACCOUNT = "work" }`.

**Other MCP clients**: run `wacli-mcp` as a stdio server.

## Tools

| Tool | What it does |
| --- | --- |
| `whatsapp_status` | Checks that wacli is installed, paired, and has a readable store. |
| `list_chats` | Chats by recent activity, with JIDs and unread counts. Filters: `query`, `unread_only`. |
| `list_messages` | A chat's messages, newest first. Filters: `before`, `after`, `from_them_only`. |
| `search_messages` | Full-text search, optionally within one chat or from one sender. |
| `get_message_context` | The messages around one message. |
| `search_contacts` | Contacts by name or number, with their JIDs. |
| `send_text` | Sends a text message, optionally as a reply, after you confirm it. |

All tools except `send_text` call wacli with `--read-only`. Results label message content as untrusted: text written by other people, not instructions.

## Confirmation behavior

The confirmation shows the chat name and JID, the reply target, the length, and the full text. Invisible characters such as zero-width spaces and text-direction overrides are shown as `<U+XXXX>` so that hidden content can't slip past you.

What happens when the client can't ask you, checked against the clients' source code and with headless test runs:

| Client and mode | Result |
| --- | --- |
| Claude Code, interactive | Shows the confirmation dialog. |
| Claude Code, `-p` (headless), including `--permission-mode bypassPermissions` | Not sent. |
| Codex TUI with `approval_policy = "on-request"` | Shows the confirmation form. |
| Codex with `--dangerously-bypass-approvals-and-sandbox`, or `codex exec` | Not sent. |
| A client without elicitation support | Not sent; the tool returns an error. |

To rely on your client's own tool approval instead, set `WACLI_MCP_CONFIRM_SENDS=false`. Then any mode that skips the client's approval also sends without asking.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WACLI_MCP_WACLI_PATH` | `wacli` | Path to the wacli executable. |
| `WACLI_MCP_ACCOUNT` | unset | wacli named account (`--account`). |
| `WACLI_MCP_STORE` | unset | wacli store directory (`--store`). |
| `WACLI_MCP_READ_ONLY` | `false` | Hide `send_text`. |
| `WACLI_MCP_CONFIRM_SENDS` | `true` | Ask before every send. See above before turning it off. |
| `WACLI_MCP_LINK_PREVIEWS` | `false` | Let wacli fetch link previews for URLs in sent messages. |
| `WACLI_MCP_SYNC_BEFORE_READ` | `true` | Run `wacli sync --once` before reads when the store may be stale. |
| `WACLI_MCP_SYNC_MAX_AGE_SECONDS` | `60` | Minimum time between those syncs. |
| `WACLI_MCP_SYNC_IDLE_EXIT_SECONDS` | `4` | How long a sync waits for new events before it stops. |
| `WACLI_MCP_SYNC_TIMEOUT_SECONDS` | `45` | Upper bound for one sync. |
| `WACLI_MCP_COMMAND_TIMEOUT_SECONDS` | `60` | Upper bound for any other wacli command. |
| `WACLI_MCP_MAX_OUTPUT_CHARS` | `60000` | Truncate longer tool results. |

## Security

An agent with this server has your private messages, reads text that anyone can send you, and can send messages. Simon Willison calls that combination [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/): a crafted incoming message can try to make the agent leak data. Invariant Labs [demonstrated it](https://invariantlabs.ai/blog/whatsapp-mcp-exploited) against an earlier WhatsApp MCP server, where an incoming message made the agent send the user's contacts to the attacker.

What wacli-mcp does about it:

- Sends need your confirmation through the server, so a client in an auto-approve mode can't skip it.
- The confirmation shows the real recipient and the full text, with hidden characters made visible.
- Reads run with wacli's `--read-only` flag, and results label message content as untrusted.
- No network listener, and link previews are off by default.

What it does not protect against:

- An agent that can run arbitrary shell commands can run `wacli send` directly, or read `~/.wacli`, without going through this server. Keep your agent's shell approvals on in sessions that use WhatsApp.
- Anything the agent reads goes to the model provider that runs the agent.
- `~/.wacli/session.db` holds your linked-device keys. Anyone who copies it can act as that device. To revoke it, open WhatsApp on your phone, go to **Settings > Linked devices**, and log the device out.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

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
