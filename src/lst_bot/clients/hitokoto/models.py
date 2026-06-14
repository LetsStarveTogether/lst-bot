from __future__ import annotations

from datetime import datetime
from textwrap import wrap
from typing import Annotated, override
from uuid import UUID

from pydantic import BaseModel, Field

from .enums import HitokotoType

QUOTE_WIDTH = 12
QUOTE_SPACE = "\u3000"
QUOTE_CORNERS = "┌┐└┘"
SOURCE_QUOTES = "《》"


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
        quote_lines = wrap(self.hitokoto.strip(), width=QUOTE_WIDTH) or [""]
        quote = "\n".join([
            f"{QUOTE_CORNERS[0]}{QUOTE_SPACE * QUOTE_WIDTH}{QUOTE_CORNERS[1]}",
            *(f"{QUOTE_SPACE}{line}" for line in quote_lines),
            f"{QUOTE_CORNERS[2]}{QUOTE_SPACE * QUOTE_WIDTH}{QUOTE_CORNERS[3]}",
        ])
        author = self.from_who.strip() if self.from_who else ""
        source = self.from_.strip()
        if SOURCE_QUOTES[0] not in source or SOURCE_QUOTES[1] not in source:
            source = f"{SOURCE_QUOTES[0]}{source}{SOURCE_QUOTES[1]}"
        signature = f"{author}{source}" if author else source
        signature_line = f"—— {signature}"
        signature_indent = QUOTE_SPACE * max(QUOTE_WIDTH + 4 - len(signature_line), 0)
        return f"{quote}\n{signature_indent}{signature_line}"


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
