from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from math import isfinite
from typing import Annotated, Literal, Self, cast, override

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Discriminator,
    Field,
    JsonValue,
    RootModel,
    StrictFloat,
    StrictInt,
    StrictStr,
    Tag,
    TypeAdapter,
    model_validator,
)

from .base import Model
from .enums import MsgSegmentType

_STANDARD_SEGMENT_TYPES = frozenset(
    segment_type
    for segment_type in MsgSegmentType
    if segment_type != MsgSegmentType.EXTENSION
)


def _tag_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(key)
    return getattr(value, key, None)


def _segment_tag(value: object) -> MsgSegmentType:
    segment_type = _tag_value(value, "type")
    try:
        tag = MsgSegmentType(segment_type)
    except ValueError:
        return MsgSegmentType.EXTENSION
    if tag in _STANDARD_SEGMENT_TYPES:
        return tag
    return MsgSegmentType.EXTENSION


def _finite_number(value: float) -> int | float:
    if not isfinite(float(value)):
        msg = "message segment number must be finite"
        raise ValueError(msg)
    return value


type FiniteNumber = Annotated[
    StrictInt | StrictFloat,
    AfterValidator(_finite_number),
]


class TextSegmentData(Model):
    text: StrictStr


class MentionSegmentData(Model):
    user_id: StrictStr


class EmptySegmentData(Model):
    pass


class FileSegmentData(Model):
    file_id: StrictStr


class LocationSegmentData(Model):
    latitude: FiniteNumber
    longitude: FiniteNumber
    title: StrictStr
    content: StrictStr


class ReplySegmentData(Model):
    message_id: StrictStr
    user_id: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ExtensionSegmentData(Model):
    @model_validator(mode="after")
    def data_value(self) -> Self:
        if self.model_extra and "type" in self.model_extra:
            msg = "extension segment data must not contain type"
            raise ValueError(msg)
        return self


class TextSegment(Model):
    type: Literal[MsgSegmentType.TEXT] = MsgSegmentType.TEXT
    data: TextSegmentData


class MentionSegment(Model):
    type: Literal[MsgSegmentType.MENTION] = MsgSegmentType.MENTION
    data: MentionSegmentData


class MentionAllSegment(Model):
    type: Literal[MsgSegmentType.MENTION_ALL] = MsgSegmentType.MENTION_ALL
    data: EmptySegmentData


class ImageSegment(Model):
    type: Literal[MsgSegmentType.IMAGE] = MsgSegmentType.IMAGE
    data: FileSegmentData


class VoiceSegment(Model):
    type: Literal[MsgSegmentType.VOICE] = MsgSegmentType.VOICE
    data: FileSegmentData


class AudioSegment(Model):
    type: Literal[MsgSegmentType.AUDIO] = MsgSegmentType.AUDIO
    data: FileSegmentData


class VideoSegment(Model):
    type: Literal[MsgSegmentType.VIDEO] = MsgSegmentType.VIDEO
    data: FileSegmentData


class FileSegment(Model):
    type: Literal[MsgSegmentType.FILE] = MsgSegmentType.FILE
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
    | Annotated[ImageSegment, Tag(MsgSegmentType.IMAGE)]
    | Annotated[VoiceSegment, Tag(MsgSegmentType.VOICE)]
    | Annotated[AudioSegment, Tag(MsgSegmentType.AUDIO)]
    | Annotated[VideoSegment, Tag(MsgSegmentType.VIDEO)]
    | Annotated[FileSegment, Tag(MsgSegmentType.FILE)]
    | Annotated[LocationSegment, Tag(MsgSegmentType.LOCATION)]
    | Annotated[ReplySegment, Tag(MsgSegmentType.REPLY)]
    | Annotated[ExtensionSegment, Tag(MsgSegmentType.EXTENSION)],
    Discriminator(_segment_tag),
]

type MsgSegmentInput = MsgSegment | Mapping[str, JsonValue]


class Msg(RootModel[list[MsgSegment]]):
    root: list[MsgSegment] = Field(default_factory=list)

    @classmethod
    def t(cls, text: str) -> Msg:
        return cls([
            TextSegment(
                data=TextSegmentData(text=text),
            ),
        ])

    @classmethod
    def mention(cls, user_id: str, message: MsgInput = None) -> Msg:
        return cls([
            MentionSegment(data=MentionSegmentData(user_id=user_id)),
            *cls.from_input(message).root,
        ])

    @classmethod
    def reply(
        cls,
        message_id: str,
        message: MsgInput = None,
        *,
        user_id: str | None = None,
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
        return _MSG_INPUT_ADAPTER.validate_python(value)

    def __len__(self) -> int:
        return len(self.root)

    def __getitem__(self, index: int) -> MsgSegment:
        return self.root[index]

    @override
    def __iter__(self) -> Iterator[MsgSegment]:  # ty: ignore[invalid-method-override]
        return iter(self.root)

    def _text(self, *, strip: bool) -> str:
        text = "".join(
            segment.data.text if isinstance(segment, TextSegment) else ""
            for segment in self.root
        )
        if strip:
            return text.strip()
        return text

    @property
    def text(self) -> str:
        return self._text(strip=True)

    @override
    def __str__(self) -> str:
        return self._text(strip=False)

    def append(self, segment: MsgSegmentInput | str) -> None:
        self.extend(segment)

    def extend(self, segments: MsgInput) -> None:
        self.root.extend(_MSG_INPUT_ADAPTER.validate_python(segments).root)


type MsgInput = Msg | MsgSegmentInput | Iterable[MsgSegmentInput] | str | None


def _msg_input_value(value: object) -> object:
    if isinstance(value, Msg):
        return value.root
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": MsgSegmentType.TEXT, "data": {"text": value}}]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Iterable):
        return value
    msg = "message input must be a string, segment object, or segment list"
    raise TypeError(msg)


type MsgValue = Annotated[Msg, BeforeValidator(_msg_input_value)]

_MSG_INPUT_ADAPTER = TypeAdapter(MsgValue)


__all__ = [
    "AudioSegment",
    "EmptySegmentData",
    "ExtensionSegment",
    "ExtensionSegmentData",
    "FileSegment",
    "FileSegmentData",
    "FiniteNumber",
    "ImageSegment",
    "LocationSegment",
    "LocationSegmentData",
    "MentionAllSegment",
    "MentionSegment",
    "MentionSegmentData",
    "Msg",
    "MsgInput",
    "MsgSegment",
    "MsgSegmentInput",
    "ReplySegment",
    "ReplySegmentData",
    "TextSegment",
    "TextSegmentData",
    "VideoSegment",
    "VoiceSegment",
]
