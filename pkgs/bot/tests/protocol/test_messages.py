from __future__ import annotations

import pytest
from bot import Msg, MsgSegmentType
from bot.protocol.msg import TextSegment, TextSegmentData
from pydantic import JsonValue, ValidationError

SEGMENT_CASES: tuple[object, ...] = (
    pytest.param(
        {"type": "text", "data": {"text": "hello"}},
        MsgSegmentType.TEXT,
        id="text",
    ),
    pytest.param(
        {"type": "mention", "data": {"user_id": "42"}},
        MsgSegmentType.MENTION,
        id="mention",
    ),
    pytest.param(
        {"type": "mention_all", "data": {}},
        MsgSegmentType.MENTION_ALL,
        id="mention-all",
    ),
    pytest.param(
        {"type": "image", "data": {"file_id": "image-1"}},
        MsgSegmentType.IMAGE,
        id="image",
    ),
    pytest.param(
        {"type": "voice", "data": {"file_id": "voice-1"}},
        MsgSegmentType.VOICE,
        id="voice",
    ),
    pytest.param(
        {"type": "audio", "data": {"file_id": "audio-1"}},
        MsgSegmentType.AUDIO,
        id="audio",
    ),
    pytest.param(
        {"type": "video", "data": {"file_id": "video-1"}},
        MsgSegmentType.VIDEO,
        id="video",
    ),
    pytest.param(
        {"type": "file", "data": {"file_id": "file-1"}},
        MsgSegmentType.FILE,
        id="file",
    ),
    pytest.param(
        {
            "type": "location",
            "data": {
                "latitude": 39.9,
                "longitude": 116.4,
                "title": "Beijing",
                "content": "China",
            },
        },
        MsgSegmentType.LOCATION,
        id="location",
    ),
    pytest.param(
        {
            "type": "reply",
            "data": {"message_id": "message-1", "user_id": "42"},
        },
        MsgSegmentType.REPLY,
        id="reply",
    ),
    pytest.param(
        {
            "type": "qq.face",
            "data": {"id": "1", "animated": True, "metadata": None},
        },
        "qq.face",
        id="extension",
    ),
)


@pytest.mark.parametrize(("payload", "segment_type"), SEGMENT_CASES)
def test_each_message_segment_variant_round_trips_json(
    payload: dict[str, JsonValue],
    segment_type: MsgSegmentType | str,
) -> None:
    message = Msg.model_validate([payload])

    assert message[0].type == segment_type
    assert Msg.model_validate_json(message.model_dump_json()) == message


def test_message_normalization_is_idempotent() -> None:
    payload = [
        {"type": "text", "data": {"text": "hello"}},
        {"type": "mention", "data": {"user_id": "42"}},
        {"type": "vendor.segment", "data": {"value": None}},
    ]

    normalized = Msg.model_validate(payload).model_dump(mode="json", by_alias=True)

    assert Msg.model_validate(normalized).model_dump(mode="json", by_alias=True) == (
        normalized
    )


def test_message_text_and_mutation_helpers_use_protocol_segments() -> None:
    message = Msg.mention("42", " hello")
    message.append({"type": "text", "data": {"text": " world"}})
    message.extend([
        {"type": "mention_all", "data": {}},
        {"type": "text", "data": {"text": "!"}},
    ])

    assert str(message) == " hello world!"
    assert message.text == "hello world!"
    assert [segment.type for segment in message] == [
        MsgSegmentType.MENTION,
        MsgSegmentType.TEXT,
        MsgSegmentType.TEXT,
        MsgSegmentType.MENTION_ALL,
        MsgSegmentType.TEXT,
    ]


def test_reply_helper_omits_null_user_id_on_wire() -> None:
    message = Msg.reply("message-1", "received")

    assert message.model_dump(mode="json", by_alias=True) == [
        {"type": "reply", "data": {"message_id": "message-1"}},
        {"type": "text", "data": {"text": "received"}},
    ]


def test_message_accepts_text_segment_model_and_preserves_whitespace() -> None:
    message = Msg.from_input(TextSegment(data=TextSegmentData(text="  hello  ")))
    message.append(TextSegment(data=TextSegmentData(text="world")))

    assert str(message) == "  hello  world"
    assert message.text == "hello  world"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([{"data": {}}], id="missing-type"),
        pytest.param([{"type": 1, "data": {}}], id="non-string-type"),
        pytest.param([{"type": "text"}], id="missing-data"),
        pytest.param([{"type": "text", "data": []}], id="non-object-data"),
        pytest.param([{"type": "text", "data": {}}], id="text-missing-text"),
        pytest.param(
            [{"type": "qq.face", "data": {"type": "reserved"}}],
            id="extension-reserved-data-type",
        ),
    ],
)
def test_message_rejects_invalid_discriminator_or_shape(payload: object) -> None:
    with pytest.raises((TypeError, ValidationError)):
        Msg.model_validate(payload)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
    ],
)
def test_location_rejects_non_finite_coordinates(value: float) -> None:
    with pytest.raises(ValidationError):
        Msg.model_validate([
            {
                "type": "location",
                "data": {
                    "latitude": value,
                    "longitude": 0,
                    "title": "invalid",
                    "content": "invalid",
                },
            },
        ])


def test_extension_segment_rejects_nested_non_finite_number() -> None:
    with pytest.raises(ValidationError):
        Msg.model_validate([
            {
                "type": "vendor.segment",
                "data": {"values": [float("nan")]},
            },
        ])
