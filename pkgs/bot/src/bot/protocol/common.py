from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr

from .base import Model
from .constants import NAME_PATTERN

type _Name = Annotated[
    StrictStr,
    Field(pattern=rf"^(?:{NAME_PATTERN.pattern})$"),
]


class BotSelf(Model):
    model_config = ConfigDict(frozen=True)

    platform: _Name
    user_id: StrictStr

    def __str__(self) -> str:
        return f"{self.platform}:{self.user_id}"


class Version(Model):
    impl: _Name
    version: StrictStr
    onebot_version: Literal["12"]

    def __str__(self) -> str:
        return f"{self.impl}@{self.version} ob{self.onebot_version}"


class BotStatus(Model):
    self_: BotSelf = Field(alias="self")
    online: StrictBool

    def __str__(self) -> str:
        state = "online" if self.online else "offline"
        return f"{self.self_} {state}"


class Status(Model):
    good: StrictBool
    bots: list[BotStatus]

    def __str__(self) -> str:
        state = "good" if self.good else "bad"
        return f"{state} bots={len(self.bots)}"


__all__ = [
    "BotSelf",
    "BotStatus",
    "Status",
    "Version",
]
