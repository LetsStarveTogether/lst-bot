from enum import Flag, StrEnum
from typing import Annotated

from pydantic import StringConstraints


class Platform(Flag):
    Steam = 0b000001
    PSN = 0b000010
    Rail = 0b000100
    # QQGAME is defunct; Klei moved users to TGP/WeGame, now queried as Rail.
    # https://forums.kleientertainment.com/forums/topic/115578-retrieving-dst-server-data/#findComment-1306033
    # QQGame = 0b001000
    XBone = 0b010000
    Switch = 0b100000


type Region = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)+$"),
]


class Season(StrEnum):
    AUTUMN = "autumn"
    WINTER = "winter"
    SPRING = "spring"
    SUMMER = "summer"


class VersionType(StrEnum):
    RELEASE = "Release"
    TEST = "Test"
