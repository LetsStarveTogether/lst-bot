import pytest
from bot import Msg, MsgSegmentType
from bot.protocol.msg import TextSegment, TextSegmentData
from pydantic import JsonValue, ValidationError

SEGMENT_CASES: tuple[dict[str, JsonValue], ...] = (
    {"type": "text", "data": {"text": "hello"}},
    {"type": "mention", "data": {"user_id": "42"}},
    {"type": "mention_all", "data": {}},
    {"type": "image", "data": {"file_id": "image-1"}},
    {"type": "voice", "data": {"file_id": "voice-1"}},
    {"type": "audio", "data": {"file_id": "audio-1"}},
    {"type": "video", "data": {"file_id": "video-1"}},
    {"type": "file", "data": {"file_id": "file-1"}},
    {
        "type": "location",
        "data": {
            "latitude": 39.9,
            "longitude": 116.4,
            "title": "Beijing",
            "content": "China",
        },
    },
    {
        "type": "reply",
        "data": {"message_id": "message-1", "user_id": "42"},
    },
    {
        "type": "qq.face",
        "data": {"id": "1", "animated": True, "metadata": None},
    },
)


@pytest.mark.parametrize(
    "payload",
    SEGMENT_CASES,
    ids=[str(payload["type"]) for payload in SEGMENT_CASES],
)
def test_each_message_segment_variant_round_trips_json(
    payload: dict[str, JsonValue],
) -> None:
    message = Msg.model_validate([payload])

    assert message[0].type == payload["type"]
    assert Msg.model_validate_json(message.model_dump_json()) == message


def test_message_text_uses_protocol_segments() -> None:
    message = Msg.from_input([
        {"type": "mention", "data": {"user_id": "42"}},
        {"type": "text", "data": {"text": " hello"}},
        {"type": "text", "data": {"text": " world"}},
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

    assert str(message) == "  hello  "
    assert message.text == "hello"


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
