from collections.abc import Iterable, Iterator, Mapping
from typing import Annotated, Literal, override

from pydantic import (
    BeforeValidator,
    Discriminator,
    Field,
    JsonValue,
    RootModel,
    StrictFloat,
    StrictStr,
    Tag,
)
from pydantic.experimental.missing_sentinel import MISSING

from .base import Model, _field_value
from .enums import MsgSegmentType


def _segment_tag(value: object) -> MsgSegmentType:
    segment_type = _field_value(value, "type")
    return (
        MsgSegmentType(segment_type)
        if segment_type in MsgSegmentType
        else MsgSegmentType.EXTENSION
    )


class TextSegmentData(Model):
    text: StrictStr


class MentionSegmentData(Model):
    user_id: StrictStr


class FileSegmentData(Model):
    file_id: StrictStr


class LocationSegmentData(Model):
    latitude: StrictFloat
    longitude: StrictFloat
    title: StrictStr
    content: StrictStr


class ReplySegmentData(Model):
    message_id: StrictStr
    user_id: StrictStr | MISSING = MISSING


class ExtensionSegmentData(Model):
    type: MISSING = MISSING


class TextSegment(Model):
    type: Literal[MsgSegmentType.TEXT] = MsgSegmentType.TEXT
    data: TextSegmentData


class MentionSegment(Model):
    type: Literal[MsgSegmentType.MENTION] = MsgSegmentType.MENTION
    data: MentionSegmentData


class MentionAllSegment(Model):
    type: Literal[MsgSegmentType.MENTION_ALL] = MsgSegmentType.MENTION_ALL
    data: Model


class MediaSegment(Model):
    type: Literal[
        MsgSegmentType.IMAGE,
        MsgSegmentType.VOICE,
        MsgSegmentType.AUDIO,
        MsgSegmentType.VIDEO,
        MsgSegmentType.FILE,
    ]
    data: FileSegmentData


class LocationSegment(Model):
    type: Literal[MsgSegmentType.LOCATION] = MsgSegmentType.LOCATION
    data: LocationSegmentData


class ReplySegment(Model):
    type: Literal[MsgSegmentType.REPLY] = MsgSegmentType.REPLY
    data: ReplySegmentData


class ExtensionSegment(Model):
    type: StrictStr
    data: ExtensionSegmentData


type MsgSegment = Annotated[
    Annotated[TextSegment, Tag(MsgSegmentType.TEXT)]
    | Annotated[MentionSegment, Tag(MsgSegmentType.MENTION)]
    | Annotated[MentionAllSegment, Tag(MsgSegmentType.MENTION_ALL)]
    | Annotated[MediaSegment, Tag(MsgSegmentType.IMAGE)]
    | Annotated[MediaSegment, Tag(MsgSegmentType.VOICE)]
    | Annotated[MediaSegment, Tag(MsgSegmentType.AUDIO)]
    | Annotated[MediaSegment, Tag(MsgSegmentType.VIDEO)]
    | Annotated[MediaSegment, Tag(MsgSegmentType.FILE)]
    | Annotated[LocationSegment, Tag(MsgSegmentType.LOCATION)]
    | Annotated[ReplySegment, Tag(MsgSegmentType.REPLY)]
    | Annotated[ExtensionSegment, Tag(MsgSegmentType.EXTENSION)],
    Discriminator(_segment_tag),
]

type MsgSegmentInput = MsgSegment | Mapping[str, JsonValue]


class Msg(RootModel[list[MsgSegment]]):
    root: list[MsgSegment] = Field(default_factory=list)

    @classmethod
    def reply(
        cls,
        message_id: str,
        message: MsgInput = (),
        *,
        user_id: str | MISSING = MISSING,
    ) -> Msg:
        return cls([
            ReplySegment(
                data=ReplySegmentData(
                    message_id=message_id,
                    user_id=user_id,
                ),
            ),
            *cls.from_input(message).root,
        ])

    @classmethod
    def from_input(cls, value: MsgInput) -> Msg:
        return cls.model_validate(_msg_input_value(value))

    def __len__(self) -> int:
        return len(self.root)

    def __getitem__(self, index: int) -> MsgSegment:
        return self.root[index]

    @override
    def __iter__(self) -> Iterator[MsgSegment]:  # ty: ignore[invalid-method-override]
        return iter(self.root)

    @property
    def text(self) -> str:
        return str(self).strip()

    @override
    def __str__(self) -> str:
        return "".join(
            segment.data.text if isinstance(segment, TextSegment) else ""
            for segment in self.root
        )


type MsgInput = Msg | MsgSegmentInput | Iterable[MsgSegmentInput] | str


def _msg_input_value(value: object) -> object:
    if isinstance(value, Msg):
        return value.model_dump()
    if isinstance(value, Model):
        return [value.model_dump()]
    if value is None:
        return value
    if isinstance(value, str):
        return [{"type": MsgSegmentType.TEXT, "data": {"text": value}}]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Iterable):
        return value
    msg = "message input must be a string, segment object, or segment list"
    raise TypeError(msg)


type MsgValue = Annotated[Msg, BeforeValidator(_msg_input_value)]
