# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue for a security problem.

Include what an attacker can do, the steps to reproduce it, and the wacli-mcp, wacli, and MCP client versions involved. You should get a reply within a week.

If the report is valid, the fix ships in a new release, and the [changelog](CHANGELOG.md) says what changed. For a vulnerability that puts users at risk, we publish a GitHub security advisory with the release and credit you in it, unless you prefer not to be named. Please give us a reasonable time to fix the problem before you disclose it.

## Supported versions

wacli-mcp is below 1.0, and only the latest release gets security fixes. Check which version you run with `uv tool list` or `pipx list`, and upgrade with `uv tool upgrade wacli-mcp` or `pipx upgrade wacli-mcp`.

## Scope

In scope:

- With `WACLI_MCP_SEND_CONFIRMATION=server`: a way to send a WhatsApp message through `send_text` without the MCP client returning an explicit confirmation for that exact recipient and text, or more than one message per confirmation.
- A confirmation prompt that shows something different from what is sent, for example through hidden characters or a crafted chat name.
- A `send_text` call that reaches a recipient other than the `to` it was given, apart from WhatsApp's own identifier routing noted below.
- Argument injection into the wacli command line.
- Read tools that send messages, mark messages read, or change account or chat state. The pre-read sync updates the local copy and can send delivery receipts, as any linked device does; with `WACLI_MCP_READ_ONLY=true` it does not run at all.

Out of scope, because the server can't control them:

- In the default `client` confirmation mode, an agent deciding to send. The client's tool approval is the control there.
- An MCP client that answers the `server` confirmation without showing it to a person.
- WhatsApp returning a different identifier for the same account when wacli sends to a phone-number JID.
- An agent with shell access running wacli directly or reading the wacli store.
- A model following instructions inside a message it read. The server labels message content as untrusted, but the model decides what to do with it.
- Bugs in wacli, whatsmeow, or the MCP client. Please report those to their projects.
