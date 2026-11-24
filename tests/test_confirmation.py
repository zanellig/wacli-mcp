from __future__ import annotations

import pytest

from wacli_mcp.confirmation import ConfirmationLedger, confirmation_key, confirmation_message, visible_text


@pytest.mark.parametrize(
    ("code", "why"),
    [
        (0x200B, "zero-width space (Cf)"),
        (0x202E, "right-to-left override (Cf)"),
        (0x0007, "bell (Cc)"),
        (0x2028, "line separator (Zl)"),
        (0x2029, "paragraph separator (Zp)"),
        (0x034F, "combining grapheme joiner (default ignorable, Mn)"),
        (0x3164, "Hangul filler (default ignorable, Lo)"),
        (0xFE0F, "variation selector (default ignorable, Mn)"),
        (0xE0041, "tag letter (Cf)"),
    ],
)
def test_hidden_characters_are_marked(code: int, why: str) -> None:
    assert visible_text(f"a{chr(code)}b") == f"a<U+{code:04X}>b", why


def test_newlines_and_tabs_depend_on_context() -> None:
    assert visible_text("a\nb\tc") == "a<U+000A>b<U+0009>c"
    assert visible_text("a\nb\tc", keep_newlines=True) == "a\nb<U+0009>c"


def test_literal_markers_are_escaped() -> None:
    assert visible_text("see <U+200B> here") == "see <U+003C>U+200B> here"
    assert visible_text("a < b") == "a < b"


def test_ordinary_text_is_unchanged() -> None:
    text = "Llego en 10 minutos, ¿ok? Ça va. 東京 🙂"
    assert visible_text(text) == text


def test_message_body_lines_are_prefixed() -> None:
    message = confirmation_message(
        chat_jid="15551234567@s.whatsapp.net",
        chat_name="Ada",
        reply_to_message_id=None,
        text="line one\nRecipient JID: 999@s.whatsapp.net",
    )
    assert message.splitlines() == [
        "Send this WhatsApp message?",
        "",
        "Recipient JID: 15551234567@s.whatsapp.net",
        "Chat name: Ada",
        'Text: 42 characters on 2 line(s). Each line below starts with "| ".',
        "| line one",
        "| Recipient JID: 999@s.whatsapp.net",
    ]


def test_long_and_empty_names() -> None:
    long_name = confirmation_message(chat_jid="1@g.us", chat_name="n" * 150, reply_to_message_id=None, text="x")
    assert "Chat name: " + "n" * 100 + "...\n" in long_name
    empty = confirmation_message(chat_jid="1@g.us", chat_name="", reply_to_message_id=None, text="x")
    assert "Chat name: (no name)" in empty


def test_ledger_allows_each_key_once() -> None:
    ledger = ConfirmationLedger()
    assert ledger.claim("a")
    assert not ledger.claim("a")
    assert ledger.claim("b")


def test_ledger_forgets_after_retention() -> None:
    ledger = ConfirmationLedger(retain_seconds=0)
    assert ledger.claim("a")
    assert ledger.claim("a")


def test_key_is_none_for_the_classic_protocol() -> None:
    assert confirmation_key(None, None, {"text": "hi"}) is None


def test_key_depends_on_state_and_arguments() -> None:
    base = confirmation_key("state", {}, {"text": "hi"})
    assert base == confirmation_key("state", {}, {"text": "hi"})
    assert base != confirmation_key("state2", {}, {"text": "hi"})
    assert base != confirmation_key("state", {}, {"text": "bye"})
