# WhatsApp via wacli

This plugin adds the `whatsapp` MCP server to Claude Code or Codex. Install [uv](https://docs.astral.sh/uv/) and pair [wacli](https://github.com/openclaw/wacli) with `wacli auth` before using it. The plugin downloads wacli-mcp from PyPI on first use.

In Claude Code, run `/plugin install wacli-mcp --marketplace zanellig/wacli-mcp`. For Codex, see the [plugin install command](https://github.com/zanellig/wacli-mcp#2-install-the-plugin).

Ask your agent to run `whatsapp_status` to check the connection. See the [project README](https://github.com/zanellig/wacli-mcp#sending) for send confirmations, read-only mode, and how the server handles your messages.
