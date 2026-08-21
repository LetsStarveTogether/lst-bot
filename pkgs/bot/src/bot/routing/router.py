from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from operator import attrgetter

from diwire import Injected, ResolverProtocol

from bot.core.di import InjectionContext, call_with_injection, inject
from bot.protocol.enums import EventKind
from bot.protocol.events import GroupMessageEvent, MessageEvent, UserEvent

from .cmd import Cmd

_ROUTE_PRIORITY_KEY = attrgetter("priority")


@dataclass(frozen=True, slots=True, kw_only=True)
class _EventRoute:
    event_type: EventKind | None = None
    predicates: tuple[Callable, ...]
    priority: int
    block: bool
    handler: Callable
    name: str

    async def matches(
        self,
        context: InjectionContext,
        resolver: ResolverProtocol,
    ) -> bool:
        for predicate in self.predicates:
            if not await call_with_injection(predicate, context, resolver):
                return False
        return True

    def __str__(self) -> str:
        event_type = self.event_type or "*"
        return f"{self.name}:{event_type}"


class EventRouter:
    def __init__(self) -> None:
        self.routes: list[_EventRoute] = []

    def on_event(
        self,
        event_type: EventKind | None = None,
        *predicates: Callable,
        priority: int = 1,
        block: bool = False,
        name: str | None = None,
    ) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.routes.append(
                _EventRoute(
                    event_type=event_type,
                    predicates=tuple(inject(predicate) for predicate in predicates),
                    priority=priority,
                    block=block,
                    handler=inject(handler),
                    name=name or getattr(handler, "__name__", "handler"),
                ),
            )
            self.routes.sort(key=_ROUTE_PRIORITY_KEY)
            return handler

        return decorator

    def on_msg(
        self,
        *predicates: Callable,
        priority: int = 1,
        block: bool = False,
        name: str | None = None,
    ) -> Callable:
        return self.on_event(
            EventKind.MESSAGE,
            *predicates,
            priority=priority,
            block=block,
            name=name,
        )

    def on_cmd(
        self,
        cmd: str,
        *predicates: Callable,
        aliases: Iterable[str] = (),
        priority: int = 1,
        block: bool = True,
        name: str | None = None,
    ) -> Callable:
        cmds = (cmd, *aliases)

        def cmd_predicate(
            event: Injected[MessageEvent],
            context: Injected[InjectionContext],
        ) -> bool:
            text = event.message.text
            for prefix in context.bot.cmd_prefixes:
                for item in cmds:
                    token = f"{prefix}{item}"
                    if text == token or (
                        text.startswith(token) and text[len(token)].isspace()
                    ):
                        arg = text[len(token) :].strip()
                        context.cmd = Cmd(raw=token, arg=arg)
                        return True
            return False

        return self.on_msg(
            cmd_predicate,
            *predicates,
            priority=priority,
            block=block,
            name=name or cmd,
        )

    def add_router(self, router: EventRouter) -> None:
        self.routes.extend(router.routes)
        self.routes.sort(key=_ROUTE_PRIORITY_KEY)


def admin_permission(
    event: Injected[UserEvent],
    context: Injected[InjectionContext],
) -> bool:
    if event.user_id in context.bot.admin_ids.get(event.self_.platform, ()):
        return True

    sender = (event.model_extra or {}).get("sender")
    role = sender.get("role") if isinstance(sender, Mapping) else None
    return (
        isinstance(event, GroupMessageEvent)
        and isinstance(sender, Mapping)
        and sender.get("user_id") == event.user_id
        and isinstance(role, str)
        and role in {"admin", "owner"}
    )
