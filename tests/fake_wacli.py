"""Stand-in for the wacli executable. Logs each call and prints canned JSON envelopes.

Environment:
    FAKE_WACLI_LOG: file that receives one JSON array (the argv) per call.
    FAKE_WACLI_FAIL_SYNC: when set, `sync` fails as if another process held the store lock.
"""

from __future__ import annotations

import json
import os
import sys

GLOBAL_FLAGS_WITH_VALUE = {"--account", "--store"}
GLOBAL_SWITCHES = {"--json", "--read-only"}

MESSAGE = {
    "ChatJID": "15551234567@s.whatsapp.net",
    "ChatName": "Ada",
    "MsgID": "MSG1",
    "SenderJID": "15551234567@s.whatsapp.net",
    "SenderName": "Ada",
    "Timestamp": "2026-11-24T10:00:00Z",
    "FromMe": False,
    "Text": "Can you send me the report?",
    "DisplayText": "Can you send me the report?",
    "IsForwarded": False,
    "ForwardingScore": 0,
    "ReactionToID": "",
    "ReactionEmoji": "",
    "MediaType": "",
    "MediaCaption": "",
    "Filename": "",
    "MimeType": "",
    "DirectPath": "/v/secret-path",
    "LocalPath": "",
    "DownloadedAt": "",
    "Starred": False,
    "StarredAt": "",
    "Edited": False,
    "Revoked": False,
    "DeletedForMe": False,
    "DeliveredTo": 0,
    "ReadBy": 0,
    "Snippet": "",
}


def reply(data: object = None, error: str | None = None) -> None:
    print(json.dumps({"success": error is None, "data": data, "error": error}))
    sys.exit(0 if error is None else 1)


def main() -> None:
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_WACLI_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")

    read_only = "--read-only" in argv
    rest: list[str] = []
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg in GLOBAL_FLAGS_WITH_VALUE and index + 1 < len(argv):
            skip = True
            continue
        if arg in GLOBAL_SWITCHES:
            continue
        rest.append(arg)
    command = " ".join(rest[:2])

    if rest[:1] == ["doctor"]:
        reply({"authenticated": True, "connected": False})
    if rest[:1] == ["sync"]:
        if os.environ.get("FAKE_WACLI_FAIL_SYNC"):
            reply(error="store is locked by another wacli process")
        reply({"messages_synced": 0})
    if command == "chats list":
        reply([{"jid": MESSAGE["ChatJID"], "kind": "dm", "name": "Ada", "unread": True, "unread_count": 1}])
    if command == "chats show":
        jid = rest[rest.index("--jid") + 1]
        if jid.startswith("999"):
            reply(error="chat not found")
        reply({"jid": jid, "name": "Ada"})
    if command in {"messages list", "messages search", "messages context"}:
        reply(
            {
                "fts": True,
                "messages": [MESSAGE],
                "page": {"has_more": True, "next_cursor": "c1"},
                "source": {"last_sync_at": "2026-11-24T10:01:00Z", "scope": "local_archive"},
            }
        )
    if command == "contacts search":
        reply([{"jid": MESSAGE["ChatJID"], "phone": "15551234567", "name": "Ada"}])
    if command == "send text":
        if read_only:
            reply(error="read-only mode: command would intentionally modify WhatsApp or the local store")
        reply({"sent": True, "to": rest[rest.index("--to") + 1], "id": "SENT1"})
    reply(error=f"fake wacli: unsupported command {rest!r}")


if __name__ == "__main__":
    main()
