from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from ipaddress import IPv4Address
from typing import Annotated

from pydantic import (
    BaseModel,
    Field,
    RootModel,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from selectolax.parser import HTMLParser, Node

from .enums import Platform, Region, Role, Season, VersionType

_VERSION_DATE_PATTERN = re.compile(r"\d{1,2}/\d{1,2}/\d{2}")
_VERSION_NUMBER_PATTERN = re.compile(r"\b\d+\b")
_PAGE_COUNT_PATTERN = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)")
_SHORT_YEAR_BASE = 2000


class Version(BaseModel):
    number: int
    type: VersionType
    date: date
    url: str
    row_id: int | None = None
    release_id: int | None = None
    is_current_release: bool = False
    is_hotfix: bool = False

    @model_validator(mode="before")
    @classmethod
    def parse_html_row(cls, value: object) -> object:
        if not isinstance(value, Node):
            return value

        link = value.css_first("a.cRelease")
        heading = value.css_first("h3.ipsType_sectionHead")
        badge = value.css_first("h3.ipsType_sectionHead span.ipsBadge")
        meta = value.css_first(".ipsDataItem_meta")
        if link is None or heading is None or badge is None or meta is None:
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
            "number": number_match.group(),
            "type": badge.text(strip=True),
            "date": date_match.group(),
            "url": link.attributes.get("href"),
            "row_id": cls.parse_optional_int(value.attributes.get("data-rowid")),
            "release_id": cls.parse_optional_int(link.attributes.get("data-releaseid")),
            "is_current_release": "data-currentrelease" in link.attributes,
            "is_hotfix": value.css_first(".cUpdate_hotfix") is not None,
        }

    @field_validator("date", mode="before")
    @classmethod
    def date_value(cls, value: object) -> object:
        if isinstance(value, str) and _VERSION_DATE_PATTERN.fullmatch(value):
            return cls.parse_date(value)
        return value

    @staticmethod
    def parse_date(value: str) -> date:
        month, day, year = (int(part) for part in value.split("/"))
        return date(_SHORT_YEAR_BASE + year, month, day)

    @staticmethod
    def parse_optional_int(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value.replace(",", ""))
        except ValueError:
            return None


class VersionPage(BaseModel):
    title: str
    page: int | None = None
    page_count: int | None = None
    followers: int | None = None
    versions: list[Version]

    @model_validator(mode="before")
    @classmethod
    def parse_html(cls, value: object) -> object:
        if isinstance(value, str):
            tree = HTMLParser(value)
        elif isinstance(value, HTMLParser):
            tree = value
        else:
            return value

        versions = sorted(
            (
                version
                for row in tree.css("li.cCmsRecord_row")
                if (version := cls.parse_version(row))
            ),
            key=lambda version: (version.date, version.number),
            reverse=True,
        )
        page, page_count = cls.parse_page_numbers(tree)
        return {
            "title": cls.parse_title(tree),
            "page": page,
            "page_count": page_count,
            "followers": cls.parse_followers(tree),
            "versions": versions,
        }

    @staticmethod
    def parse_version(row: Node) -> Version | None:
        try:
            return Version.model_validate(row)
        except ValidationError:
            return None

    @staticmethod
    def parse_title(tree: HTMLParser) -> str:
        if title := tree.css_first("h1"):
            return title.text(strip=True)
        if title := tree.css_first("title"):
            return title.text(strip=True)
        return ""

    @staticmethod
    def parse_page_numbers(tree: HTMLParser) -> tuple[int | None, int | None]:
        for pagination in tree.css(".ipsPagination"):
            if match := _PAGE_COUNT_PATTERN.search(
                pagination.text(separator=" ", strip=True),
            ):
                return int(match.group(1)), int(match.group(2))
        return None, None

    @staticmethod
    def parse_followers(tree: HTMLParser) -> int | None:
        if count := tree.css_first("[data-role='followButton'] .ipsCommentCount"):
            return Version.parse_optional_int(count.text(strip=True))
        return None


class BuildVersions(RootModel[dict[str, list[int | str]]]):
    pass


class RegionCapability(BaseModel):
    region: Annotated[str, Field(alias="Region")]


class RegionCapabilities(BaseModel):
    lobby_regions: list[RegionCapability] = Field(
        default_factory=list,
        alias="LobbyRegions",
    )


class KleiDataResponse[T](BaseModel):
    rows: list[T] = Field(default_factory=list, alias="GET")


class Player(BaseModel):
    name: str
    kuid: str
    role: Role | None = None
    steam_id: int | None = None
    ip: IPv4Address | None = None


class Secondary(BaseModel):
    id: str
    port: int | None = None
    addr: Annotated[IPv4Address | None, Field(alias="__addr")] = None
    steamid: str | None = None


class LobbyData(BaseModel):
    row_id: Annotated[str, Field(alias="__rowId")]
    name: str
    addr: Annotated[IPv4Address, Field(alias="__addr")]
    port: int
    host: str
    connected: int
    maxconnections: int
    v: int
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
    platform: Platform
    session: str
    guid: str
    intent: str
    steamroom: str
    region: Region

    tags: str | None = None
    mode: str | None = None
    season: Season | None = None
    steamid: str | None = None
    secondaries: dict[str, Secondary] | None = None

    @model_validator(mode="before")
    @classmethod
    def inject_region(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, Mapping) or "region" in value:
            return value
        if not isinstance(info.context, Mapping):
            return value
        region = info.context.get("region")
        if region is None:
            return value
        return {**value, "region": region}

    @property
    def connect_code(self) -> str:
        return f"c_connect('{self.addr}', {self.port})"


class RoomData(LobbyData):
    tick: int
    clientmodsoff: bool
    nat: int
    data: str | None = None
    worldgen: str | None = None
    mods_info: list[str | bool | None] | None = None
    players: str | None = None
    desc: str | None = None


__all__ = [
    "BuildVersions",
    "KleiDataResponse",
    "LobbyData",
    "Player",
    "RegionCapabilities",
    "RegionCapability",
    "RoomData",
    "Secondary",
    "Version",
    "VersionPage",
]
