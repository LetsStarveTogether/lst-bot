from dataclasses import dataclass

from bot import (
    Bot,
    Cmd,
    EventRouter,
    Injected,
    InjectionContext,
    Scope,
    UserEvent,
    admin_permission,
)
from bot.testing import private_message_event, recording_gateway


async def test_falsey_rule_is_not_replaced() -> None:
    class Deny:
        def __bool__(self) -> bool:
            return False

        def __call__(self) -> bool:
            return False

    bot = Bot()
    router = EventRouter()
    seen: list[str] = []

    @router.on_msg(rule=Deny())
    def protected() -> None:
        seen.append("allowed")

    bot.add_router(router)
    gateway = recording_gateway(bot)
    async with bot:
        await bot.dispatch(gateway.connection, private_message_event("hello"))

    assert seen == []


def test_admin_permission_rejects_non_string_sender_role() -> None:
    bot = Bot()
    source = private_message_event("hello")
    payload = source.model_dump(mode="json")
    payload["sender"] = {"role": []}

    assert not admin_permission(
        type(source).model_validate(payload),
        InjectionContext(bot),
    )


@dataclass(frozen=True)
class Tenant:
    user_id: str


def get_tenant(event: UserEvent) -> Tenant:
    return Tenant(event.user_id)


async def test_router_cmd_aliases_do_not_match_partial_tokens() -> None:
    bot = Bot(cmd_prefixes=("/", "!"))
    router = EventRouter()
    seen: list[str] = []

    @router.on_cmd("ping", aliases=("p",), block=True)
    def ping(cmd: Injected[Cmd]) -> None:
        seen.append(f"{cmd.raw}:{cmd.arg}")

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

    assert seen == ["!p:now"]


async def test_container_factory_dependency() -> None:
    bot = Bot()
    bot.container.add_factory(
        get_tenant,
        scope=Scope.REQUEST,
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
