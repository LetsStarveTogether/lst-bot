from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

type Region = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)+$"),
]


class VersionType(StrEnum):
    RELEASE = "Release"
    TEST = "Test"
