import re
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    OnErrorOmit,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)
from selectolax.parser import HTMLParser, Node

_VERSION_DATE_PATTERN = re.compile(r"\d{1,2}/\d{1,2}/\d{2}")
_VERSION_NUMBER_PATTERN = re.compile(r"\b\d+\b")
type Region = Annotated[
    str,
    Field(strict=True, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)+$"),
]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


class VersionType(StrEnum):
    RELEASE = "Release"
    TEST = "Test"


class Version(BaseModel):
    number: NonNegativeInt
    type: VersionType
    date: date

    @model_validator(mode="before")
    @classmethod
    def parse_html_row(cls, value: object) -> object:
        if not isinstance(value, Node):
            return value

        heading = value.css_first("h3.ipsType_sectionHead")
        badge = value.css_first("h3.ipsType_sectionHead span.ipsBadge")
        meta = value.css_first(".ipsDataItem_meta")
        if heading is None or badge is None or meta is None:
            msg = "version row is missing required nodes"
            raise ValueError(msg)

        number_match = _VERSION_NUMBER_PATTERN.search(
            heading.text(separator=" ", strip=True)
        )
        date_match = _VERSION_DATE_PATTERN.search(meta.text(separator=" ", strip=True))
        if number_match is None or date_match is None:
            msg = "version row is missing number or date"
            raise ValueError(msg)

        return {
            "number": int(number_match.group()),
            "type": badge.text(strip=True),
            "date": date.strptime(date_match.group(), "%m/%d/%y"),
        }


_VERSIONS = TypeAdapter(list[OnErrorOmit[Version]])


def _parse_versions(value: str) -> list[Version]:
    return _VERSIONS.validate_python(HTMLParser(value).css("li.cCmsRecord_row"))


class KleiDataResponse[T](BaseModel):
    rows: list[T] = Field(alias="GET")


class LobbyData(BaseModel):
    model_config = ConfigDict(strict=True)

    row_id: Annotated[str, Field(strict=True, min_length=1, alias="__rowId")]
    host: str
    connected: NonNegativeInt
    region: Region

    @model_validator(mode="before")
    @classmethod
    def inject_region(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, Mapping):
            return value
        if not isinstance(info.context, Mapping):
            return value
        region = info.context.get("region")
        if region is None:
            return value
        return {**value, "region": region}


class RoomData(BaseModel):
    model_config = ConfigDict(strict=True)

    name: str
    connected: NonNegativeInt
    maxconnections: NonNegativeInt
    password: bool
    serverpaused: bool
    season: str | None = None
    data: str | None = None

    @model_validator(mode="after")
    def validate_connected(self) -> Self:
        if self.connected > self.maxconnections:
            msg = "connected cannot exceed maxconnections"
            raise ValueError(msg)
        return self


class _RoomDataResponse(KleiDataResponse[RoomData]):
    rows: list[RoomData] = Field(alias="GET", max_length=1)
