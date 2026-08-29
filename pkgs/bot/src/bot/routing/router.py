from collections.abc import Callable, Iterable
from dataclasses import dataclass
from operator import attrgetter

from bot.core.di import Injected, InjectedCall, InjectionContext, inject
from bot.protocol.enums import EventKind
from bot.protocol.events import GroupMessageEvent, MessageEvent, UserEvent

from .cmd import Cmd

_ROUTE_PRIORITY_KEY = attrgetter("priority")


@dataclass(frozen=True, slots=True, kw_only=True)
class _EventRoute:
    event_type: EventKind | None = None
    predicates: tuple[InjectedCall, ...]
    priority: int
    block: bool
    handler: InjectedCall
    name: str

    async def matches(self, context: InjectionContext) -> bool:
        for predicate in self.predicates:
            if not await predicate(context):
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
        _name: str | None = None,
    ) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.routes.append(
                _EventRoute(
                    event_type=event_type,
                    predicates=tuple(inject(predicate) for predicate in predicates),
                    priority=priority,
                    block=block,
                    handler=inject(handler),
                    name=_name or getattr(handler, "__name__", type(handler).__name__),
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
    ) -> Callable:
        return self.on_event(
            EventKind.MESSAGE,
            *predicates,
            priority=priority,
            block=block,
        )

    def on_cmd(
        self,
        cmd: str,
        *predicates: Callable,
        aliases: Iterable[str] = (),
        priority: int = 1,
        block: bool = True,
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

        return self.on_event(
            EventKind.MESSAGE,
            cmd_predicate,
            *predicates,
            priority=priority,
            block=block,
            _name=cmd,
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
    match sender:
        case {"user_id": user_id, "role": "admin" | "owner"}:
            return isinstance(event, GroupMessageEvent) and user_id == event.user_id
    return False
