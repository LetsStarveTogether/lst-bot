from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictStr

from .base import Model
from .constants import NAME_PATTERN

type _Name = Annotated[
    StrictStr,
    Field(pattern=rf"^(?:{NAME_PATTERN.pattern})$"),
]


class BotSelf(Model):
    platform: _Name
    user_id: StrictStr

    def __str__(self) -> str:
        return f"{self.platform}:{self.user_id}"


class Version(Model):
    impl: _Name
    version: StrictStr
    onebot_version: Literal["12"]


class BotStatus(Model):
    self_: BotSelf = Field(alias="self")
    online: StrictBool


class Status(Model):
    good: StrictBool
    bots: list[BotStatus]
