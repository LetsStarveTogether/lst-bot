from __future__ import annotations

from dataclasses import dataclass

from lst_bot import (
    Bot,
    Cmd,
    EventRoute,
    EventRouter,
    Injected,
    InjectionContext,
    Lifetime,
    Scope,
    State,
    UserEvent,
)
from tests.conftest import EventFactory, RecordingGatewayFactory


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


async def test_router_cmd_uses_diwire_injected_service(
    recording_gateway: RecordingGatewayFactory,
    make_event: EventFactory,
) -> None:
    bot = Bot()
    bot.container.add_instance(Repository("repo"), provides=Repository)
    bot.container.add(Service)
    router = EventRouter(name="admin")

    @router.on_cmd("ping", block=True)
    def ping(service: Injected[Service], cmd: Injected[Cmd]) -> str:
        return service.render(cmd.arg)

    bot.add_router(router)
    gateway = recording_gateway(bot)

    results = await bot.dispatch(
        gateway.connection,
        make_event("/ping ok", user_id="42"),
    )

    assert results[0].route.name == "admin.ping"
    assert results[0].values == ["repo:ok"]


async def test_router_cmd_aliases_do_not_match_partial_tokens(
    recording_gateway: RecordingGatewayFactory,
    make_event: EventFactory,
) -> None:
    bot = Bot(cmd_prefixes=("/", "!"))
    router = EventRouter()

    @router.on_cmd("ping", aliases=("p",), block=True)
    def ping(cmd: Injected[Cmd]) -> str:
        return f"{cmd.name}:{cmd.raw}:{cmd.arg}"

    bot.add_router(router)
    gateway = recording_gateway(bot)

    partial_results = await bot.dispatch(
        gateway.connection,
        make_event("/pingpong now", event_id="partial"),
    )
    alias_results = await bot.dispatch(
        gateway.connection,
        make_event("!p now", event_id="alias"),
    )

    assert partial_results == []
    assert alias_results[0].values == ["p:!p:now"]


async def test_router_cmd_blocks_by_default(
    recording_gateway: RecordingGatewayFactory,
    make_event: EventFactory,
) -> None:
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
    gateway = recording_gateway(bot)

    await bot.dispatch(gateway.connection, make_event("/ping"))

    assert seen == ["command"]


async def test_context_route_and_cmd_are_resolved_from_current_route(
    recording_gateway: RecordingGatewayFactory,
    make_event: EventFactory,
) -> None:
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
    gateway = recording_gateway(bot)

    results = await bot.dispatch(gateway.connection, make_event("hello"))

    assert [result.values[0] for result in results] == [
        "first:first",
        "second:second",
    ]


async def test_container_factory_dependency(
    recording_gateway: RecordingGatewayFactory,
    make_event: EventFactory,
) -> None:
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
    gateway = recording_gateway(bot)

    results = await bot.dispatch(
        gateway.connection,
        make_event("hello", user_id="7"),
    )

    assert results[0].values == ["7"]


async def test_route_dependencies_run_before_handler(
    recording_gateway: RecordingGatewayFactory,
    make_event: EventFactory,
) -> None:
    bot = Bot()
    router = EventRouter()

    def mark(state: Injected[State]) -> None:
        state["ready"] = True

    @router.on_msg(block=True, dependencies=[mark])
    def collect(state: Injected[State]) -> str:
        return "ready" if state["ready"] else "missing"

    bot.add_router(router)
    gateway = recording_gateway(bot)

    results = await bot.dispatch(
        gateway.connection,
        make_event("hello"),
    )

    assert results[0].values == ["ready"]
