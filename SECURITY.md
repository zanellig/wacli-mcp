# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue for a security problem.

Include what an attacker can do, the steps to reproduce it, and the wacli-mcp, wacli, and MCP client versions involved. You should get a reply within a week.

## Scope

In scope:

- A way to send a WhatsApp message through `send_text` without the MCP client returning an explicit confirmation for that exact recipient and text, or more than one message per confirmation.
- A confirmation prompt that shows something different from what is sent, for example through hidden characters or a crafted chat name.
- Argument injection into the wacli command line.
- Read tools that send messages, mark messages read, or change account or chat state. The pre-read sync updates the local copy and can send delivery receipts, as any linked device does; with `WACLI_MCP_READ_ONLY=true` it does not run at all.

Out of scope, because the server can't control them:

- An MCP client that answers the confirmation without showing it to a person.
- WhatsApp returning a different identifier for the same account when wacli sends to a phone-number JID.
- An agent with shell access running wacli directly or reading the wacli store.
- A model following instructions inside a message it read. The server labels message content as untrusted, but the model decides what to do with it.
- Bugs in wacli, whatsmeow, or the MCP client. Please report those to their projects.
