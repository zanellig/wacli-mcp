"""What the user sees before a send, and the guard that lets each confirmation send only once."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Control, format, private-use, surrogate, unassigned, and line/paragraph separator characters.
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
# Default_Ignorable_Code_Point characters outside those categories (Unicode DerivedCoreProperties).
# Most fonts draw them as nothing: a combining grapheme joiner, Hangul fillers, Mongolian and
# variation selectors, and the tag block.
_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0000, 0xE0FFF),
)
_MAX_NAME_CHARS = 100
# Longer than the MCP SDK's default 600-second lifetime for multi-round request state.
_RETAIN_SECONDS = 900.0


def _is_hidden(char: str) -> bool:
    if unicodedata.category(char) in _HIDDEN_CATEGORIES:
        return True
    code = ord(char)
    return any(low <= code <= high for low, high in _IGNORABLE_RANGES)


def visible_text(text: str, *, keep_newlines: bool = False) -> str:
    """Show every character that could hide content as `<U+XXXX>`.

    A literal `<U+` in the input is written as `<U+003C>U+`, so every marker in the output
    stands for a real hidden character.
    """
    parts: list[str] = []
    for index, char in enumerate(text):
        if keep_newlines and char == "\n":
            parts.append(char)
        elif _is_hidden(char):
            parts.append(f"<U+{ord(char):04X}>")
        elif char == "<" and text.startswith("U+", index + 1):
            parts.append("<U+003C>")
        else:
            parts.append(char)
    return "".join(parts)


def confirmation_message(
    *,
    chat_jid: str,
    chat_name: str | None,
    reply_to_message_id: str | None,
    text: str,
) -> str:
    """The prompt shown before a send.

    `chat_jid` and `reply_to_message_id` must already be validated: they appear verbatim.
    The chat name comes from the local store, which other people can influence, so it is
    rendered on one line. Each line of the message body starts with `| `, so the body cannot
    imitate the header fields.
    """
    if chat_name is None:
        name = "(not in your synced chats)"
    elif not chat_name:
        name = "(no name)"
    else:
        name = visible_text(chat_name[:_MAX_NAME_CHARS]) + ("..." if len(chat_name) > _MAX_NAME_CHARS else "")
    lines = [
        "Send this WhatsApp message?",
        "",
        f"Recipient JID: {chat_jid}",
        f"Chat name: {name}",
    ]
    if reply_to_message_id:
        lines.append(f"Reply to message ID: {reply_to_message_id}")
    body = visible_text(text, keep_newlines=True).split("\n")
    lines.append(f'Text: {len(text)} characters on {len(body)} line(s). Each line below starts with "| ".')
    lines += [f"| {line}" for line in body]
    return "\n".join(lines)


def _jsonable(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json", by_alias=True) if callable(dump) else value


def confirmation_key(
    request_state: str | None,
    input_responses: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
) -> str | None:
    """Identify one answered confirmation in the multi-round (2026-07-28) protocol.

    Returns `None` for the classic protocol, where each tool call asks its own question.
    """
    if request_state is None and not input_responses:
        return None
    payload = {
        "state": request_state,
        "responses": {key: _jsonable(value) for key, value in (input_responses or {}).items()},
        "arguments": dict(arguments),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class ConfirmationLedger:
    """Remembers answered confirmations that already started a send.

    In the multi-round protocol the client resubmits the original call with the user's
    answer. A client that retries that request (for example after losing the result)
    would otherwise send the same message again on one confirmation.
    """

    retain_seconds: float = _RETAIN_SECONDS
    _claimed: dict[str, float] = field(default_factory=dict)

    def claim(self, key: str) -> bool:
        """Return True the first time `key` is seen within `retain_seconds`, else False.

        There is no `await` here, so concurrent retries on one event loop cannot both win.
        """
        now = time.monotonic()
        self._claimed = {k: t for k, t in self._claimed.items() if now - t < self.retain_seconds}
        if key in self._claimed:
            return False
        self._claimed[key] = now
        return True
