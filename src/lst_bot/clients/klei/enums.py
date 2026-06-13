from enum import Flag, StrEnum


class Platform(Flag):
    Steam = 0b000001
    PSN = 0b000010
    Rail = 0b000100
    XBone = 0b010000
    Switch = 0b100000


class Region(StrEnum):
    US_EAST = "us-east-1"
    EU_CENTRAL = "eu-central-1"
    AP_SOUTHEAST = "ap-southeast-1"
    AP_EAST = "ap-east-1"


class Role(StrEnum):
    WILSON = "wilson"
    WILLOW = "willow"
    WENDY = "wendy"
    WOLFGANG = "wolfgang"
    WX78 = "wx78"
    WICKERBOTTOM = "wickerbottom"
    WES = "wes"
    WAXWELL = "waxwell"
    WOODIE = "woodie"
    WATHGRITHR = "wathgrithr"
    WEBBER = "webber"
    WINONA = "winona"
    WORTOX = "wortox"
    WORMWOOD = "wormwood"
    WARLY = "warly"
    WURT = "wurt"
    WALTER = "walter"
    WANDA = "wanda"
    WONKEY = "wonkey"
    UNKNOWN = ""


class Season(StrEnum):
    AUTUMN = "autumn"
    WINTER = "winter"
    SPRING = "spring"
    SUMMER = "summer"


class VersionType(StrEnum):
    RELEASE = "Release"
    TEST = "Test"


__all__ = [
    "Platform",
    "Region",
    "Role",
    "Season",
    "VersionType",
]
