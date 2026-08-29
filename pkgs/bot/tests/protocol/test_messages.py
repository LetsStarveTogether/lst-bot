import pytest
from bot import Msg, MsgSegmentType
from bot.protocol import msg as msg_models
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

SEGMENT_CLASSES = {
    "text": msg_models.TextSegment,
    "mention": msg_models.MentionSegment,
    "mention_all": msg_models.MentionAllSegment,
    "location": msg_models.LocationSegment,
    "reply": msg_models.ReplySegment,
    "qq.face": msg_models.ExtensionSegment,
    **dict.fromkeys(
        ("image", "voice", "audio", "video", "file"),
        msg_models.MediaSegment,
    ),
}

REQUIRED_SEGMENT_DATA_FIELDS = {
    "text": ("text",),
    "mention": ("user_id",),
    "image": ("file_id",),
    "location": ("latitude", "longitude", "title", "content"),
    "reply": ("message_id",),
}


@pytest.mark.parametrize(
    "payload",
    SEGMENT_CASES,
    ids=[str(payload["type"]) for payload in SEGMENT_CASES],
)
def test_each_message_segment_variant_serializes_its_wire_shape(
    payload: dict[str, JsonValue],
) -> None:
    message = Msg.model_validate([payload])

    assert type(message[0]) is SEGMENT_CLASSES[str(payload["type"])]
    assert message.model_dump(mode="json") == [payload]


@pytest.mark.parametrize(
    ("segment_type", "field"),
    [
        (segment_type, field)
        for segment_type, fields in REQUIRED_SEGMENT_DATA_FIELDS.items()
        for field in fields
    ],
)
def test_message_segments_require_each_wire_data_field(
    segment_type: str,
    field: str,
) -> None:
    payload = next(
        payload for payload in SEGMENT_CASES if payload["type"] == segment_type
    )
    data = payload["data"]
    assert isinstance(data, dict)

    with pytest.raises(ValidationError):
        Msg.model_validate([
            {
                "type": segment_type,
                "data": {key: value for key, value in data.items() if key != field},
            }
        ])


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


def test_reply_helper_omits_absent_user_id_on_wire() -> None:
    message = Msg.reply("message-1", "received")

    assert message.model_dump(mode="json") == [
        {"type": "reply", "data": {"message_id": "message-1"}},
        {"type": "text", "data": {"text": "received"}},
    ]


def test_message_accepts_text_segment_model_and_preserves_whitespace() -> None:
    segment = msg_models.TextSegment(data=msg_models.TextSegmentData(text="  hello  "))
    message = Msg.from_input(segment)

    assert message[0] is segment
    assert Msg.from_input(message) is message
    assert str(message) == "  hello  "
    assert message.text == "hello"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([{"data": {}}], id="missing-type"),
        pytest.param([{"type": 1, "data": {}}], id="non-string-type"),
        pytest.param([{"type": "text"}], id="missing-data"),
        pytest.param([{"type": "text", "data": []}], id="non-object-data"),
        pytest.param(
            [{"type": "qq.face", "data": {"type": "reserved"}}],
            id="extension-reserved-data-type",
        ),
        pytest.param(
            [{"type": "reply", "data": {"message_id": "1", "user_id": None}}],
            id="reply-null-user-id",
        ),
    ],
)
def test_message_rejects_invalid_discriminator_or_shape(payload: object) -> None:
    with pytest.raises(ValidationError):
        Msg.model_validate(payload)
