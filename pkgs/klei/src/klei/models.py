import re
from collections.abc import Mapping
from datetime import date
from ipaddress import IPv4Address
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    OnErrorOmit,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)
from selectolax.parser import HTMLParser, Node

from .enums import Platform, Region, VersionType

_VERSION_DATE_PATTERN = re.compile(r"\d{1,2}/\d{1,2}/\d{2}")
_VERSION_NUMBER_PATTERN = re.compile(r"\b\d+\b")
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]


def _platform_value(value: object) -> Platform:
    if isinstance(value, Platform):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Platform(value)
    msg = "platform must be an integer"
    raise ValueError(msg)


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
    return sorted(
        _VERSIONS.validate_python(HTMLParser(value).css("li.cCmsRecord_row")),
        key=lambda version: (version.date, version.number),
        reverse=True,
    )


class KleiDataResponse[T](BaseModel):
    rows: list[T] = Field(default_factory=list, alias="GET")


class Secondary(BaseModel):
    model_config = ConfigDict(strict=True)

    id: str
    port: Port | None = None
    addr: Annotated[IPv4Address | None, Field(alias="__addr")] = None
    steamid: str | None = None


class LobbyData(BaseModel):
    model_config = ConfigDict(strict=True)

    row_id: Annotated[str, Field(strict=True, min_length=1, alias="__rowId")]
    name: str
    addr: Annotated[IPv4Address, Field(alias="__addr")]
    port: Port
    host: str
    connected: NonNegativeInt
    maxconnections: NonNegativeInt
    v: NonNegativeInt
    allownewplayers: bool
    clanonly: bool
    clienthosted: bool
    dedicated: bool
    fo: bool
    lanonly: bool
    mods: bool
    password: bool
    pvp: bool
    serverpaused: bool
    platform: Annotated[Platform, BeforeValidator(_platform_value)]
    session: str
    guid: str
    intent: str
    steamroom: str
    region: Region

    tags: str | None = None
    mode: str | None = None
    season: str | None = None
    steamid: str | None = None
    secondaries: dict[str, Secondary] | None = None

    @model_validator(mode="after")
    def validate_connected(self) -> Self:
        if self.connected > self.maxconnections:
            msg = "connected cannot exceed maxconnections"
            raise ValueError(msg)
        return self

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


class RoomData(LobbyData):
    tick: NonNegativeInt
    clientmodsoff: bool
    nat: NonNegativeInt
    data: str | None = None
    worldgen: str | None = None
    mods_info: list[str | bool | None] | None = None
    players: str | None = None
    desc: str | None = None
