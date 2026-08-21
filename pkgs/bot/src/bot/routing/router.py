from collections.abc import Callable, Iterable
from operator import attrgetter

from diwire import Injected

from bot.core.di import InjectionContext
from bot.protocol.enums import EventKind
from bot.protocol.events import MessageEvent

from .cmd import Cmd
from .route import EventRoute
from .rule import Permission, Rule

ROUTE_PRIORITY_KEY = attrgetter("priority")


class EventRouter:
    def __init__(
        self,
        *,
        name: str | None = None,
        dependencies: Iterable[Callable] = (),
    ) -> None:
        self.name = name
        self.dependencies = list(dependencies)
        self.routes: list[EventRoute] = []

    def on_event(
        self,
        event_type: EventKind | None = None,
        *,
        rule: Rule | Callable | None = None,
        permission: Permission | Callable | None = None,
        priority: int = 1,
        block: bool = False,
        name: str | None = None,
        dependencies: Iterable[Callable] = (),
    ) -> Callable:
        route_rule = rule if isinstance(rule, Rule) else Rule(rule or (lambda: True))
        route_permission = (
            permission
            if isinstance(permission, Permission)
            else Permission(permission or (lambda: True))
        )
        route_dependencies = list(dependencies)

        def decorator(handler: Callable) -> Callable:
            route_name = name or getattr(handler, "__name__", "handler")
            if self.name:
                route_name = f"{self.name}.{route_name}"
            self.routes.append(
                EventRoute(
                    event_type=event_type,
                    rule=route_rule,
                    permission=route_permission,
                    priority=priority,
                    block=block,
                    handler=handler,
                    name=route_name,
                    dependencies=[*self.dependencies, *route_dependencies],
                ),
            )
            self.routes.sort(key=ROUTE_PRIORITY_KEY)
            return handler

        return decorator

    def on_msg(
        self,
        *,
        rule: Rule | Callable | None = None,
        permission: Permission | Callable | None = None,
        priority: int = 1,
        block: bool = False,
        name: str | None = None,
        dependencies: Iterable[Callable] = (),
    ) -> Callable:
        return self.on_event(
            EventKind.MESSAGE,
            rule=rule,
            permission=permission,
            priority=priority,
            block=block,
            name=name,
            dependencies=dependencies,
        )

    def on_cmd(
        self,
        cmd: str,
        *,
        aliases: Iterable[str] = (),
        rule: Rule | Callable | None = None,
        permission: Permission | Callable | None = None,
        priority: int = 1,
        block: bool = True,
        name: str | None = None,
        dependencies: Iterable[Callable] = (),
    ) -> Callable:
        cmds = (cmd, *aliases)

        def cmd_rule(
            event: Injected[MessageEvent],
            context: Injected[InjectionContext],
        ) -> bool:
            text = event.message.text
            for prefix in context.bot.cmd_prefixes:
                for item in cmds:
                    token = f"{prefix}{item}"
                    if text == token or text.startswith(f"{token} "):
                        arg = text[len(token) :].strip()
                        context.cmd = Cmd(name=item, raw=token, arg=arg)
                        return True
            return False

        return self.on_msg(
            rule=Rule(cmd_rule) & rule,
            permission=permission,
            priority=priority,
            block=block,
            name=name or cmd,
            dependencies=dependencies,
        )

    def add_router(self, router: EventRouter) -> None:
        self.routes.extend(router.routes)
        self.routes.sort(key=ROUTE_PRIORITY_KEY)
