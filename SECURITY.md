# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue for a security problem.

Include what an attacker can do, the steps to reproduce it, and the wacli-mcp, wacli, and MCP client versions involved. You should get a reply within a week.

## Scope

In scope:

- A way to send a WhatsApp message through `send_text` without the user confirming that exact recipient and text.
- A confirmation prompt that shows something different from what is sent, for example through hidden characters.
- Argument injection into the wacli command line.
- Read tools that change WhatsApp or the local store.

Out of scope, because the server can't control them:

- An agent with shell access running wacli directly or reading `~/.wacli`.
- A model following instructions inside a message it read. The server labels message content as untrusted, but the model decides what to do with it.
- Bugs in wacli, whatsmeow, or the MCP client. Please report those to their projects.
