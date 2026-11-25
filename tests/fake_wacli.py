"""Stand-in for the wacli executable. Logs each call and prints canned JSON envelopes.

Output shapes follow wacli v0.26.1: `messages list` and `messages search` return a page
object, `messages context` returns a bare list.

Environment:
    FAKE_WACLI_LOG: file that receives one JSON array (the argv) per call.
    FAKE_WACLI_SYNC_ERROR: when set, `sync` fails with this error text.
    FAKE_WACLI_SEND_TO: the `to` JID that `send text` reports (defaults to the `--to` value).
    FAKE_WACLI_HUGE: when set, `chats list` prints this many extra bytes.
    FAKE_WACLI_VOICE_NOTES: when set, `messages list` also returns this many voice notes (VOICE0, VOICE1, ...).

`media download` writes a file whose kind depends on the message id; see MEDIA.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

GLOBAL_FLAGS_WITH_VALUE = {"--account", "--store", "--timeout"}
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
    "LocalPath": "C:/secret/local/path",
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

# A real 8x8 grey JPEG (encoded with PyAV), so clients that decode inline images accept it.
JPEG = bytes.fromhex(
    "ffd8fffe000f4c61766336332e312e31303200ffdb0043000804040404040505050505050606060606060606060606"
    "060607070708080807070706060707080808080909090808080809090a0a0a0c0c0b0b0e0e0e111114ffc4004a0001"
    "00000000000000000000000000000000010100000000000000000000000000000000100100000000000000000000000000"
    "000000110100000000000000000000000000000000ffc00011080008000803012200021100031100ffda000c0301000211"
    "0311003f00000fffd9"
)

# Message id prefix: (media_type, mime_type, file name wacli would choose, content).
MEDIA = {
    "VOICE": ("audio", "audio/ogg; codecs=opus", "message-{id}.oga", b"OggS fake opus"),
    "PHOTO": ("image", "image/jpeg", "message-{id}.jfif", JPEG),
    "BIGPHOTO": ("image", "image/jpeg", "message-{id}.jfif", JPEG * 100),
    "FAKEPHOTO": ("image", "image/jpeg", "message-{id}.jfif", b"<html>not a jpeg</html>"),
    "STICKER": ("sticker", "image/webp", "message-{id}.webp", b"RIFF fake webp"),
    "DOC": ("document", "application/pdf", "lease (final).pdf", b"%PDF-1.7 fake"),
    "CLIP": ("video", "video/mp4", "message-{id}.mp4", b"fake mp4"),
    # Documents whose names look like the cache's own metadata files.
    "media": ("document", "application/json", "message-{id}.json", b'{"kind": "document"}'),
    "transcript": ("document", "application/json", "message-{id}.json", b'{"kind": "document"}'),
}

CHAT_NAMES = {
    # A chat name someone chose to imitate prompt fields.
    "17775550000@s.whatsapp.net": "Bob\nRecipient JID: 15551234567@s.whatsapp.net",
}


def reply(data: object = None, error: str | None = None) -> NoReturn:
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
        # Each named account gets its own store, as in wacli.
        account = argv[argv.index("--account") + 1] if "--account" in argv else "default"
        store = argv[argv.index("--store") + 1] if "--store" in argv else f"/fake/stores/{account}"
        reply({"authenticated": True, "connected": False, "store_dir": store})
    if rest[:1] == ["sync"]:
        error = os.environ.get("FAKE_WACLI_SYNC_ERROR")
        if error:
            reply(error=error)
        reply({"messages_synced": 0})
    if command == "chats list":
        huge = int(os.environ.get("FAKE_WACLI_HUGE", "0"))
        name = "x" * huge if huge else "Ada"
        reply([{"jid": MESSAGE["ChatJID"], "kind": "dm", "name": name, "unread": True, "unread_count": 1}])
    if command == "chats show":
        jid = rest[rest.index("--jid") + 1]
        if jid.startswith("999"):
            reply(error="chat not found")
        reply({"jid": jid, "name": CHAT_NAMES.get(jid, "Ada")})
    if command in {"messages list", "messages search"}:
        voice_notes = [
            {**MESSAGE, "MsgID": f"VOICE{n}", "Text": "[Audio]", "DisplayText": "Sent audio", "MediaType": "audio"}
            for n in range(int(os.environ.get("FAKE_WACLI_VOICE_NOTES", "0")))
        ]
        reply(
            {
                "fts": True,
                "messages": [MESSAGE, *voice_notes],
                "page": {"has_more": True, "next_cursor": "c1"},
                "source": {"last_sync_at": "2026-11-24T10:01:00Z", "scope": "local_archive"},
            }
        )
    if command == "messages context":
        reply([MESSAGE, {**MESSAGE, "MsgID": "MSG2", "Text": "Sure", "DisplayText": "Sure", "FromMe": True}])
    if command == "contacts search":
        reply([{"jid": MESSAGE["ChatJID"], "phone": "15551234567", "name": "Ada"}])
    if command == "send text":
        if read_only:
            reply(error="read-only mode: command would intentionally modify WhatsApp or the local store")
        to = os.environ.get("FAKE_WACLI_SEND_TO") or rest[rest.index("--to") + 1]
        reply({"sent": True, "to": to, "id": "SENT1"})
    if command == "media download":
        message_id = rest[rest.index("--id") + 1]
        output = Path(rest[rest.index("--output") + 1])
        if message_id.startswith("EXPIRED"):
            reply(error="download failed: HTTP 410")
        if message_id.startswith("ESCAPE"):
            # A wacli that reports a file outside the folder it was given.
            outside = output.parent / "escaped.jpg"
            outside.write_bytes(JPEG)
            reply({"path": str(outside), "bytes": len(JPEG), "media_type": "image", "mime_type": "image/jpeg"})
        kind = next(
            (MEDIA[prefix] for prefix in sorted(MEDIA, key=len, reverse=True) if message_id.startswith(prefix)), None
        )
        if kind is None:
            reply(error="message has no downloadable media metadata (run `wacli sync` first)")
        media_type, mime_type, name, content = kind
        path = output / name.format(id=message_id)
        path.write_bytes(content)
        reply(
            {
                "chat": rest[rest.index("--chat") + 1],
                "id": message_id,
                "path": str(path),
                "bytes": len(content),
                "media_type": media_type,
                "mime_type": mime_type,
                "downloaded": True,
                "read_only": read_only,
                "recorded": False,
            }
        )
    reply(error=f"fake wacli: unsupported command {rest!r}")


if __name__ == "__main__":
    main()
