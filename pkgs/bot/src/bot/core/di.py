from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING

from diwire import (
    Container,
    DependencyRegistrationPolicy,
    Lifetime,
    ResolverProtocol,
    Scope,
    resolver_context,
)

from bot.gateways import Connection, Gateway
from bot.protocol.events import Event

if TYPE_CHECKING:
    from bot.routing.cmd import Cmd
    from bot.routing.route import EventRoute

    from .bot import Bot

type State = dict[str, object]

inject = resolver_context.inject(
    dependency_registration_policy=DependencyRegistrationPolicy.IGNORE,
    auto_open_scope=False,
)


@asynccontextmanager
async def request_scope(container: Container) -> AsyncIterator[ResolverProtocol]:
    async with container.compile().enter_scope(Scope.REQUEST) as resolver:  # ty: ignore[invalid-context-manager]
        yield resolver


@dataclass(slots=True)
class InjectionContext:
    bot: Bot
    gateway: Gateway | None = None
    connection: Connection | None = None
    event: Event | None = None
    state: State | None = None
    route: EventRoute | None = None
    cmd: Cmd | None = None


_CURRENT_CONTEXT: ContextVar[InjectionContext | None] = ContextVar(
    "bot_di_context",
    default=None,
)


def current_injection_context() -> InjectionContext:
    context = _CURRENT_CONTEXT.get()
    if context is None:
        msg = "No injection context is active"
        raise TypeError(msg)
    return context


async def call_with_injection(
    func: Callable,
    context: InjectionContext,
    resolver: ResolverProtocol,
) -> object:
    with _CURRENT_CONTEXT.set(context):
        value = func(diwire_resolver=resolver)
        if isawaitable(value):
            return await value
        return value


def register_context_providers(
    container: Container,
) -> None:
    def bind_event_provider(event_type: type[Event]) -> Callable[[], Event]:
        def provide_event() -> Event:
            event = current_injection_context().event
            if not isinstance(event, event_type):
                msg = f"Current event is not {event_type.__name__}"
                raise TypeError(msg)
            return event

        return provide_event

    from bot.routing.cmd import Cmd
    from bot.routing.route import EventRoute

    container.add_factory(
        current_injection_context,
        provides=InjectionContext,
        scope=Scope.REQUEST,
        lifetime=Lifetime.TRANSIENT,
    )
    container.add_factory(
        _state_from_context,
        provides=State,
        scope=Scope.REQUEST,
        lifetime=Lifetime.SCOPED,
    )
    container.add_factory(
        _cmd_from_context,
        provides=Cmd,
        scope=Scope.REQUEST,
        lifetime=Lifetime.TRANSIENT,
    )
    container.add_factory(
        _route_from_context,
        provides=EventRoute,
        scope=Scope.REQUEST,
        lifetime=Lifetime.TRANSIENT,
    )
    container.add_factory(
        _connection_from_context,
        provides=Connection,
        scope=Scope.REQUEST,
        lifetime=Lifetime.SCOPED,
    )
    for event_type in _event_types(Event):
        container.add_factory(
            bind_event_provider(event_type),
            provides=event_type,
            scope=Scope.REQUEST,
            lifetime=Lifetime.SCOPED,
        )


def _state_from_context() -> State:
    state = current_injection_context().state
    if state is None:
        msg = "Injection context must carry event state"
        raise TypeError(msg)
    return state


def _cmd_from_context() -> Cmd:
    from bot.routing.cmd import Cmd

    cmd = current_injection_context().cmd
    if not isinstance(cmd, Cmd):
        msg = "Injection context has no command"
        raise TypeError(msg)
    return cmd


def _route_from_context() -> EventRoute:
    from bot.routing.route import EventRoute

    route = current_injection_context().route
    if not isinstance(route, EventRoute):
        msg = "Injection context has no route"
        raise TypeError(msg)
    return route


def _connection_from_context() -> Connection:
    connection = current_injection_context().connection
    if not isinstance(connection, Connection):
        msg = "Injection context has no connection"
        raise TypeError(msg)
    return connection


def _event_types(root: type[Event]) -> Iterator[type[Event]]:
    seen: set[type[Event]] = set()
    stack = [root]
    while stack:
        event_type = stack.pop()
        if event_type in seen:
            continue
        seen.add(event_type)
        yield event_type
        stack.extend(event_type.__subclasses__())
