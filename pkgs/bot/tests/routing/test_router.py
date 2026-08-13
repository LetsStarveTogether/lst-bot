from __future__ import annotations

from dataclasses import dataclass

from bot import (
    Bot,
    Cmd,
    EventPayload,
    EventRoute,
    EventRouter,
    Injected,
    InjectionContext,
    Lifetime,
    PrivateMessageEvent,
    Scope,
    State,
    UserEvent,
)
from bot.testing import RecordingGateway


@dataclass(frozen=True)
class Repository:
    value: str


class Service:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def render(self, value: str) -> str:
        return f"{self.repository.value}:{value}"


@dataclass(frozen=True)
class Tenant:
    user_id: str


def get_tenant(event: UserEvent) -> Tenant:
    return Tenant(event.user_id)


def message_event(
    text: str,
    *,
    user_id: str = "42",
    event_id: str = "evt-1",
) -> PrivateMessageEvent:
    event = EventPayload.model_validate({
        "id": event_id,
        "self": {"platform": "test", "user_id": "bot"},
        "time": 1.0,
        "type": "message",
        "detail_type": "private",
        "sub_type": "",
        "message_id": f"{event_id}-message",
        "message": [{"type": "text", "data": {"text": text}}],
        "alt_message": text,
        "user_id": user_id,
    }).root
    assert isinstance(event, PrivateMessageEvent)
    return event


def add_recording_gateway(bot: Bot) -> RecordingGateway:
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)
    return gateway


async def test_router_cmd_uses_diwire_injected_service() -> None:
    bot = Bot()
    bot.container.add_instance(Repository("repo"), provides=Repository)
    bot.container.add(Service)
    router = EventRouter(name="admin")

    @router.on_cmd("ping", block=True)
    def ping(service: Injected[Service], cmd: Injected[Cmd]) -> str:
        return service.render(cmd.arg)

    bot.add_router(router)
    gateway = add_recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            message_event("/ping ok", user_id="42"),
        )

    assert results[0].route.name == "admin.ping"
    assert results[0].values == ["repo:ok"]


async def test_router_cmd_aliases_do_not_match_partial_tokens() -> None:
    bot = Bot(cmd_prefixes=("/", "!"))
    router = EventRouter()

    @router.on_cmd("ping", aliases=("p",), block=True)
    def ping(cmd: Injected[Cmd]) -> str:
        return f"{cmd.name}:{cmd.raw}:{cmd.arg}"

    bot.add_router(router)
    gateway = add_recording_gateway(bot)

    async with bot:
        partial_results = await bot.dispatch(
            gateway.connection,
            message_event("/pingpong now", event_id="partial"),
        )
        alias_results = await bot.dispatch(
            gateway.connection,
            message_event("!p now", event_id="alias"),
        )

    assert partial_results == []
    assert alias_results[0].values == ["p:!p:now"]


async def test_router_cmd_blocks_by_default() -> None:
    bot = Bot()
    router = EventRouter()
    seen: list[str] = []

    @router.on_cmd("ping")
    def command() -> None:
        seen.append("command")

    @router.on_msg()
    def message() -> None:
        seen.append("message")

    bot.add_router(router)
    gateway = add_recording_gateway(bot)

    async with bot:
        await bot.dispatch(gateway.connection, message_event("/ping"))

    assert seen == ["command"]


async def test_context_route_and_cmd_are_resolved_from_current_route() -> None:
    bot = Bot()
    router = EventRouter()

    def first_cmd(context: Injected[InjectionContext]) -> None:
        context.cmd = Cmd(name="first", raw="/first", arg="")

    def second_cmd(context: Injected[InjectionContext]) -> None:
        context.cmd = Cmd(name="second", raw="/second", arg="")

    @router.on_msg(name="first", dependencies=[first_cmd])
    def first(route: Injected[EventRoute], cmd: Injected[Cmd]) -> str:
        return f"{route.name}:{cmd.name}"

    @router.on_msg(name="second", dependencies=[second_cmd])
    def second(route: Injected[EventRoute], cmd: Injected[Cmd]) -> str:
        return f"{route.name}:{cmd.name}"

    bot.add_router(router)
    gateway = add_recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(gateway.connection, message_event("hello"))

    assert [result.values[0] for result in results] == [
        "first:first",
        "second:second",
    ]


async def test_container_factory_dependency() -> None:
    bot = Bot()
    bot.container.add_factory(
        get_tenant,
        provides=Tenant,
        scope=Scope.REQUEST,
        lifetime=Lifetime.SCOPED,
    )
    router = EventRouter()

    @router.on_msg(block=True)
    def collect(tenant: Injected[Tenant]) -> str:
        return tenant.user_id

    bot.add_router(router)
    gateway = add_recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            message_event("hello", user_id="7"),
        )

    assert results[0].values == ["7"]


async def test_route_dependencies_run_before_handler() -> None:
    bot = Bot()
    router = EventRouter()

    def mark(state: Injected[State]) -> None:
        state["ready"] = True

    @router.on_msg(block=True, dependencies=[mark])
    def collect(state: Injected[State]) -> str:
        return "ready" if state["ready"] else "missing"

    bot.add_router(router)
    gateway = add_recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            message_event("hello"),
        )

    assert results[0].values == ["ready"]
