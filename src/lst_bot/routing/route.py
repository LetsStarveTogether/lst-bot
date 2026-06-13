from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from diwire import ResolverProtocol
from pydantic import BaseModel

from lst_bot.core.di import InjectionContext, State, call_with_injection
from lst_bot.protocol.enums import EventKind
from lst_bot.protocol.returns import ReturnAction

from .rule import Permission, Rule


@dataclass(slots=True, kw_only=True, match_args=False)
class EventRoute:
    event_type: EventKind | None = None
    rule: Rule
    permission: Permission
    priority: int
    block: bool
    handlers: list[Callable]
    name: str
    dependencies: list[Callable] = field(default_factory=list)

    def __str__(self) -> str:
        event_type = self.event_type or "*"
        return f"{self.name}:{event_type}"

    async def check(
        self,
        context: InjectionContext,
        resolver: ResolverProtocol,
    ) -> bool:
        if not await self.rule(context, resolver):
            return False
        if not await self.permission(context, resolver):
            return False
        for dependency in self.dependencies:
            await call_with_injection(dependency, context, resolver)
        return True


@dataclass(slots=True, kw_only=True, match_args=False)
class ReturnEffect:
    action: ReturnAction
    outcome: BaseModel


type DispatchEffect = ReturnEffect


@dataclass(slots=True, kw_only=True, match_args=False)
class DispatchResult:
    route: EventRoute
    values: list[object]
    state: State
    effects: list[DispatchEffect] = field(default_factory=list)
    exception: BaseException | None = None


__all__ = [
    "DispatchEffect",
    "DispatchResult",
    "EventRoute",
    "ReturnEffect",
]
