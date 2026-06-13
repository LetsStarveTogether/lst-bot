from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictBool, StrictStr, field_validator

from .base import Model
from .constants import NAME_PATTERN


class BotSelf(Model):
    model_config = ConfigDict(frozen=True)

    platform: StrictStr
    user_id: StrictStr

    def __str__(self) -> str:
        return f"{self.platform}:{self.user_id}"

    @field_validator("platform")
    @classmethod
    def platform_value(cls, value: str) -> str:
        if NAME_PATTERN.fullmatch(value) is None:
            msg = "platform must match [a-z][\\-a-z0-9]*(\\.[\\-a-z0-9]+)*"
            raise ValueError(msg)
        return value


class Version(Model):
    impl: StrictStr
    version: StrictStr
    onebot_version: Literal["12"] = "12"

    def __str__(self) -> str:
        return f"{self.impl}@{self.version} ob{self.onebot_version}"

    @field_validator("impl")
    @classmethod
    def impl_value(cls, value: str) -> str:
        if NAME_PATTERN.fullmatch(value) is None:
            msg = "implementation name must match [a-z][\\-a-z0-9]*(\\.[\\-a-z0-9]+)*"
            raise ValueError(msg)
        return value


class BotStatus(Model):
    self_: BotSelf = Field(alias="self")
    online: StrictBool

    def __str__(self) -> str:
        state = "online" if self.online else "offline"
        return f"{self.self_} {state}"


class Status(Model):
    good: StrictBool
    bots: list[BotStatus] = Field(default_factory=list)

    def __str__(self) -> str:
        state = "good" if self.good else "bad"
        return f"{state} bots={len(self.bots)}"


__all__ = [
    "BotSelf",
    "BotStatus",
    "Status",
    "Version",
]
