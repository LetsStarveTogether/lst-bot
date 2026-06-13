from __future__ import annotations

import pytest
from pydantic import ValidationError

from lst_bot import Msg, MsgSegmentType
from lst_bot.protocol.msg import TextSegment, TextSegmentData
from tests.conftest import EventFactory


def test_msg_parses_protocol_message_segments() -> None:
    msg = Msg.model_validate(
        [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "mention", "data": {"user_id": "123"}},
            {"type": "text", "data": {"text": " world"}},
        ],
    )

    assert msg.text == "hello world"
    assert len(msg) == 3


def test_msg_text_strips_by_default() -> None:
    msg = Msg.from_input("  hello  ")

    assert msg.text == "hello"
    assert str(msg) == "  hello  "


def test_msg_mention_and_reply_prepend_protocol_segments() -> None:
    mention = Msg.mention("123", " hello")
    reply = Msg.reply("msg-1", " received", user_id="123")

    assert mention.model_dump(mode="json", by_alias=True) == [
        {"type": "mention", "data": {"user_id": "123"}},
        {"type": "text", "data": {"text": " hello"}},
    ]
    assert reply.model_dump(mode="json", by_alias=True) == [
        {
            "type": "reply",
            "data": {"message_id": "msg-1", "user_id": "123"},
        },
        {"type": "text", "data": {"text": " received"}},
    ]


def test_msg_iterates_segments() -> None:
    msg = Msg.t("hello")

    assert [segment.type for segment in msg] == [MsgSegmentType.TEXT]


def test_msg_segment_type_defaults_to_protocol_tag() -> None:
    segment = TextSegment(data=TextSegmentData(text="hello"))

    assert segment.type == MsgSegmentType.TEXT
    assert segment.model_dump(
        mode="json",
        by_alias=True,
    ) == {
        "type": "text",
        "data": {"text": "hello"},
    }


def test_msg_append_uses_message_input() -> None:
    msg = Msg()
    msg.append("hello")
    msg.append({"type": "text", "data": {"text": " world"}})

    assert msg.text == "hello world"


def test_msg_extend_appends_message_input() -> None:
    msg = Msg.t("hello")
    msg.extend([
        {"type": "mention", "data": {"user_id": "123"}},
        {"type": "text", "data": {"text": " world"}},
    ])

    assert [segment.type for segment in msg] == [
        MsgSegmentType.TEXT,
        MsgSegmentType.MENTION,
        MsgSegmentType.TEXT,
    ]
    assert msg.text == "hello world"


def test_event_message_uses_msg_model(make_event: EventFactory) -> None:
    event = make_event("  hello world  ", user_id="u1")

    assert str(event.message) == "  hello world  "
    assert event.message.text == "hello world"


def test_msg_segments_use_standard_and_extension_discriminators() -> None:
    segments = Msg.model_validate([
        {"type": "qq.face", "data": {"id": "1"}},
    ]).root

    assert segments[0].type == "qq.face"
    with pytest.raises(ValidationError):
        Msg.model_validate([{"type": "text", "data": {}}])
    with pytest.raises(ValidationError):
        Msg.model_validate([
            {"type": "qq.face", "data": {"type": "reserved"}},
        ])
