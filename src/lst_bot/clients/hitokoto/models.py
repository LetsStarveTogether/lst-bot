from __future__ import annotations

from datetime import datetime
from typing import Annotated, override
from uuid import UUID

from pydantic import BaseModel, Field

from .enums import HitokotoType


class Hitokoto(BaseModel):
    id: int
    uuid: UUID
    hitokoto: str
    type: HitokotoType
    from_: Annotated[str, Field(alias="from")]
    from_who: str | None
    creator: str
    creator_uid: int
    reviewer: int
    commit_from: str
    created_at: datetime

    @override
    def __str__(self) -> str:
        msg = f"{self.hitokoto}\n-- {self.from_}"
        if self.from_who:
            return f"{msg} {self.from_who}"
        return msg


class HitokotoBundleCategory(BaseModel):
    id: int
    name: str
    desc: str
    key: HitokotoType
    created_at: datetime
    updated_at: datetime
    path: str


class HitokotoBundleSentence(BaseModel):
    id: int
    uuid: UUID
    hitokoto: str
    type: HitokotoType
    from_: Annotated[str, Field(alias="from")]
    from_who: str | None
    creator: str
    creator_uid: int
    reviewer: int
    commit_from: str
    created_at: datetime
    length: int


class HitokotoBundle(BaseModel):
    protocol_version: str
    bundle_version: str
    categories: list[HitokotoBundleCategory]
    sentences: list[HitokotoBundleSentence]


class HitokotoBundleCategoryMeta(BaseModel):
    path: str
    timestamp: datetime


class HitokotoBundleSentenceMeta(BaseModel):
    name: str
    key: HitokotoType
    path: str
    timestamp: datetime


class HitokotoBundleVersion(BaseModel):
    protocol_version: str
    bundle_version: str
    updated_at: datetime
    categories: HitokotoBundleCategoryMeta
    sentences: list[HitokotoBundleSentenceMeta]


__all__ = [
    "Hitokoto",
    "HitokotoBundle",
    "HitokotoBundleCategory",
    "HitokotoBundleCategoryMeta",
    "HitokotoBundleSentence",
    "HitokotoBundleSentenceMeta",
    "HitokotoBundleVersion",
]
