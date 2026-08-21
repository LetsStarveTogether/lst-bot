from collections.abc import Callable
from dataclasses import dataclass

from diwire import ResolverProtocol

from bot.core.di import InjectionContext, inject
from bot.protocol.enums import EventKind

from .rule import Permission, Rule


@dataclass(slots=True, kw_only=True, match_args=False)
class EventRoute:
    event_type: EventKind | None = None
    rule: Rule
    permission: Permission
    priority: int
    block: bool
    handler: Callable
    name: str

    def __post_init__(self) -> None:
        self.handler = inject(self.handler)

    def __str__(self) -> str:
        event_type = self.event_type or "*"
        return f"{self.name}:{event_type}"

    async def check(
        self,
        context: InjectionContext,
        resolver: ResolverProtocol,
    ) -> bool:
        return await self.rule(context, resolver) and await self.permission(
            context,
            resolver,
        )
