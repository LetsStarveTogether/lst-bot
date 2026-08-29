from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, get_args, get_origin, get_type_hints

from bot.gateways import Connection, Gateway
from bot.protocol.events import Event

if TYPE_CHECKING:
    from bot.routing.cmd import Cmd

    from .bot import Bot

type Injected[T] = T
type InjectedCall = Callable[[InjectionContext], Awaitable[object]]


@dataclass(slots=True)
class InjectionContext:
    bot: Bot
    gateway: Gateway | None = None
    connection: Connection | None = None
    event: Event | None = None
    cmd: Cmd | None = None

    def resolve(self, dependency: type[object]) -> object:
        for value in (
            self,
            self.bot,
            self.gateway,
            self.connection,
            self.event,
            self.cmd,
        ):
            if value is not None and isinstance(value, dependency):
                return value
        try:
            return self.bot.dependencies[dependency]
        except KeyError:
            msg = f"No {dependency.__name__} is available for injection"
            raise TypeError(msg) from None


def inject(func: Callable) -> InjectedCall:
    hints = (
        get_type_hints(func, include_extras=True)
        if getattr(func, "__annotations__", None)
        else {}
    )
    dependencies: list[tuple[str, type[object]]] = []
    for name, annotation in hints.items():
        if name == "return" or get_origin(annotation) is not Injected:
            continue
        dependency = get_args(annotation)[0]
        if not isinstance(dependency, type):
            msg = f"Injected parameter {name!r} must name a runtime type"
            raise TypeError(msg)
        dependencies.append((name, dependency))

    async def call(context: InjectionContext) -> object:
        value = func(**{
            name: context.resolve(dependency) for name, dependency in dependencies
        })
        return await value if isawaitable(value) else value

    return call
