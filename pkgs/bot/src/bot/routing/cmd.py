from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Cmd:
    raw: str
    arg: str
