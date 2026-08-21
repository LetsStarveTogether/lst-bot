from collections.abc import Callable
from dataclasses import dataclass

from bot.core.di import inject
from bot.protocol.enums import EventKind

from .rule import Rule


@dataclass(slots=True, kw_only=True, match_args=False)
class EventRoute:
    event_type: EventKind | None = None
    rule: Rule
    priority: int
    block: bool
    handler: Callable
    name: str

    def __post_init__(self) -> None:
        self.handler = inject(self.handler)

    def __str__(self) -> str:
        event_type = self.event_type or "*"
        return f"{self.name}:{event_type}"
