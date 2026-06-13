from __future__ import annotations

from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING

from diwire import Container, Lifetime, ResolverProtocol, Scope, resolver_context

from lst_bot.gateways import Connection, Gateway
from lst_bot.protocol.events import (
    Event,
    FriendRequestEvent,
    GroupRequestEvent,
    MessageEvent,
)
from lst_bot.protocol.msg import Msg, MsgInput
from lst_bot.protocol.returns import ReturnAction

if TYPE_CHECKING:
    from lst_bot.routing.cmd import Cmd
    from lst_bot.routing.route import EventRoute

    from .bot import Bot

type State = dict[str, object]


@dataclass(slots=True)
class InjectionContext:
    bot: Bot
    gateway: Gateway | None = None
    connection: Connection | None = None
    event: Event | None = None
    state: State | None = None
    route: EventRoute | None = None
    cmd: Cmd | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    event: MessageEvent

    def __call__(self, message: MsgInput = None) -> ReturnAction:
        return ReturnAction.message(
            Msg.reply(
                self.event.message_id,
                message,
                user_id=self.event.user_id,
            )
        )


@dataclass(frozen=True, slots=True)
class Mention:
    event: MessageEvent

    def __call__(self, message: MsgInput = None) -> ReturnAction:
        return ReturnAction.message(Msg.mention(self.event.user_id, message))


@dataclass(frozen=True, slots=True)
class RequestResponse:
    event: FriendRequestEvent | GroupRequestEvent

    def approve(self, *, remark: str = "") -> ReturnAction:
        if isinstance(self.event, GroupRequestEvent) and remark:
            msg = "Group request approvals do not support remark"
            raise TypeError(msg)
        return ReturnAction.request(True, remark=remark)

    def reject(self, reason: str = "") -> ReturnAction:
        if isinstance(self.event, FriendRequestEvent) and reason:
            msg = "Friend request rejections do not support reason"
            raise TypeError(msg)
        return ReturnAction.request(False, reason=reason)


_CURRENT_CONTEXT: ContextVar[InjectionContext | None] = ContextVar(
    "lst_bot_di_context",
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
    token = _CURRENT_CONTEXT.set(context)
    try:
        injected = resolver_context.inject(scope=Scope.REQUEST)(func)
        value = injected(diwire_resolver=resolver)
        if isawaitable(value):
            return await value
        return value
    finally:
        _CURRENT_CONTEXT.reset(token)


def register_context_providers(
    container: Container,
    gateway_provider_types: set[type[Gateway]],
    *,
    gateway_type: type[Gateway] | None = None,
) -> None:
    def bind_event_provider(event_type: type[Event]) -> Callable[[], Event]:
        def provide_event() -> Event:
            event = current_injection_context().event
            if not isinstance(event, event_type):
                msg = f"Current event is not {event_type.__name__}"
                raise TypeError(msg)
            return event

        return provide_event

    def bind_gateway_provider(gateway_type: type[Gateway]) -> Callable[[], Gateway]:
        def provide_gateway() -> Gateway:
            gateway = current_injection_context().gateway
            if not isinstance(gateway, gateway_type):
                msg = f"Current gateway is not {gateway_type.__name__}"
                raise TypeError(msg)
            return gateway

        return provide_gateway

    if gateway_type is None:
        from lst_bot.routing.cmd import Cmd
        from lst_bot.routing.route import EventRoute

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
            _msg_from_context,
            provides=Msg,
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
        container.add_factory(
            _reply_from_context,
            provides=Reply,
            scope=Scope.REQUEST,
            lifetime=Lifetime.SCOPED,
        )
        container.add_factory(
            _mention_from_context,
            provides=Mention,
            scope=Scope.REQUEST,
            lifetime=Lifetime.SCOPED,
        )
        container.add_factory(
            _request_response_from_context,
            provides=RequestResponse,
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
        gateway_type = Gateway

    if gateway_type in gateway_provider_types:
        return
    gateway_provider_types.add(gateway_type)

    container.add_factory(
        bind_gateway_provider(gateway_type),
        provides=gateway_type,
        scope=Scope.REQUEST,
        lifetime=Lifetime.SCOPED,
    )


def _state_from_context() -> State:
    state = current_injection_context().state
    if state is None:
        msg = "Injection context must carry event state"
        raise TypeError(msg)
    return state


def _msg_from_context() -> Msg:
    event = current_injection_context().event
    if not isinstance(event, MessageEvent):
        msg = "Current event has no message"
        raise TypeError(msg)
    return event.message


def _cmd_from_context() -> Cmd:
    from lst_bot.routing.cmd import Cmd

    cmd = current_injection_context().cmd
    if not isinstance(cmd, Cmd):
        msg = "Injection context has no command"
        raise TypeError(msg)
    return cmd


def _route_from_context() -> EventRoute:
    from lst_bot.routing.route import EventRoute

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


def _message_event_from_context() -> MessageEvent:
    event = current_injection_context().event
    if not isinstance(event, MessageEvent):
        msg = "Current event has no message"
        raise TypeError(msg)
    return event


def _reply_from_context() -> Reply:
    return Reply(_message_event_from_context())


def _mention_from_context() -> Mention:
    return Mention(_message_event_from_context())


def _request_response_from_context() -> RequestResponse:
    event = current_injection_context().event
    if not isinstance(event, FriendRequestEvent | GroupRequestEvent):
        msg = "Current event is not a supported request event"
        raise TypeError(msg)
    return RequestResponse(event)


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


__all__ = [
    "InjectionContext",
    "Mention",
    "Reply",
    "RequestResponse",
    "State",
    "call_with_injection",
    "current_injection_context",
    "register_context_providers",
]
