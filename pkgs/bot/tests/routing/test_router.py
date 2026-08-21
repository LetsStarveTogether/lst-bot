from dataclasses import dataclass

import pytest
from bot import (
    Bot,
    Cmd,
    EventRouter,
    Injected,
    Lifetime,
    Permission,
    Scope,
    UserEvent,
)
from bot.testing import private_message_event, recording_gateway


@pytest.mark.parametrize("role", [[], {}], ids=["list", "mapping"])
async def test_admin_permission_rejects_non_hashable_sender_role(
    role: list[object] | dict[str, object],
) -> None:
    bot = Bot()
    router = EventRouter()
    seen: list[str] = []

    @router.on_msg(permission=Permission.admin(), block=True)
    def protected() -> None:
        seen.append("allowed")

    bot.add_router(router)
    gateway = recording_gateway(bot)
    source = private_message_event("hello")
    payload = source.model_dump(mode="json", by_alias=True)
    payload["sender"] = {"role": role}

    async with bot:
        await bot.dispatch(
            gateway.connection,
            type(source).model_validate(payload),
        )

    assert seen == []


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


async def test_router_cmd_uses_diwire_injected_service() -> None:
    bot = Bot()
    bot.container.add_instance(Repository("repo"), provides=Repository)
    bot.container.add(Service)
    router = EventRouter(name="admin")
    seen: list[str] = []

    @router.on_cmd("ping", block=True)
    def ping(service: Injected[Service], cmd: Injected[Cmd]) -> None:
        seen.append(service.render(cmd.arg))

    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        await bot.dispatch(
            gateway.connection,
            private_message_event("/ping ok", user_id="42"),
        )

    assert seen == ["repo:ok"]


async def test_router_cmd_aliases_do_not_match_partial_tokens() -> None:
    bot = Bot(cmd_prefixes=("/", "!"))
    router = EventRouter()
    seen: list[str] = []

    @router.on_cmd("ping", aliases=("p",), block=True)
    def ping(cmd: Injected[Cmd]) -> None:
        seen.append(f"{cmd.name}:{cmd.raw}:{cmd.arg}")

    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        await bot.dispatch(
            gateway.connection,
            private_message_event("/pingpong now", event_id="partial"),
        )
        await bot.dispatch(
            gateway.connection,
            private_message_event("!p now", event_id="alias"),
        )

    assert seen == ["p:!p:now"]


async def test_container_factory_dependency() -> None:
    bot = Bot()
    bot.container.add_factory(
        get_tenant,
        provides=Tenant,
        scope=Scope.REQUEST,
        lifetime=Lifetime.SCOPED,
    )
    router = EventRouter()
    seen: list[str] = []

    @router.on_msg(block=True)
    def collect(tenant: Injected[Tenant]) -> None:
        seen.append(tenant.user_id)

    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        await bot.dispatch(
            gateway.connection,
            private_message_event("hello", user_id="7"),
        )

    assert seen == ["7"]
