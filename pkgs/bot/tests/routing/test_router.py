from inspect import signature

import pytest
from bot import (
    Bot,
    Cmd,
    Event,
    EventRouter,
    GroupMessageEvent,
    Injected,
    InjectionContext,
    MessageEvent,
    admin_permission,
    configured_admin_permission,
)
from bot_test_support import private_message_event, recording_gateway


def test_injection_context_signature_is_introspectable() -> None:
    assert "bot" in signature(InjectionContext).parameters


async def test_falsey_callable_predicate_receives_injection() -> None:
    class Deny:
        def __bool__(self) -> bool:
            return False

        def __call__(self, event: Injected[MessageEvent]) -> bool:
            return event.message.text == "hello"

    bot = Bot()
    router = EventRouter()
    seen: list[str] = []

    @router.on_msg(Deny())
    def protected() -> None:
        seen.append("allowed")

    bot.add_router(router)
    gateway = recording_gateway(bot)
    async with bot:
        await bot.dispatch(gateway.connection, private_message_event("hello"))

    assert seen == ["allowed"]


def test_admin_permission_rejects_untrusted_sender_roles() -> None:
    bot = Bot()
    source = private_message_event("hello")
    payload = source.model_dump(mode="json")
    payload["sender"] = {"user_id": source.user_id, "role": "owner"}

    assert not admin_permission(
        type(source).model_validate(payload),
        InjectionContext(bot),
    )
    payload |= {
        "detail_type": "group",
        "group_id": "group",
        "sender": {"user_id": "attacker", "role": "owner"},
    }
    assert not admin_permission(
        GroupMessageEvent.model_validate(payload),
        InjectionContext(bot),
    )
    payload["sender"] = {"user_id": source.user_id, "role": []}
    assert not admin_permission(
        GroupMessageEvent.model_validate(payload),
        InjectionContext(bot),
    )


@pytest.mark.parametrize(
    ("platform", "sub_type", "anonymous", "allowed"),
    [
        ("qq", "normal", None, True),
        ("qq", "anonymous", None, False),
        ("qq", "normal", {"id": "8", "name": "anon", "flag": "f"}, False),
        ("test", "anonymous", None, True),
    ],
)
def test_anonymous_qq_messages_cannot_claim_admin_identity(
    platform: str,
    sub_type: str,
    anonymous: dict[str, str] | None,
    allowed: bool,
) -> None:
    payload = private_message_event("hello").model_dump(mode="json")
    payload |= {
        "self": {"platform": platform, "user_id": "bot"},
        "detail_type": "group",
        "sub_type": sub_type,
        "group_id": "group",
        "user_id": "80000000",
        "sender": {"user_id": "80000000", "role": "owner"},
        "anonymous": anonymous,
    }
    event = GroupMessageEvent.model_validate(payload)
    configured = InjectionContext(Bot(admin_ids={platform: {"80000000"}}))

    assert configured_admin_permission(event, configured) is allowed
    assert admin_permission(event, configured) is allowed
    assert admin_permission(event, InjectionContext(Bot())) is allowed
    assert not configured_admin_permission(event, InjectionContext(Bot()))


async def test_router_cmd_aliases_do_not_match_partial_tokens() -> None:
    bot = Bot(cmd_prefixes=("/", "!"))
    router = EventRouter()
    checked: list[str] = []
    seen: list[str] = []

    def allow(cmd: Injected[Cmd]) -> bool:
        checked.append(f"{cmd.raw}:{cmd.arg}")
        return True

    @router.on_cmd("ping", allow, aliases=("p",), block=True)
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
            private_message_event("!p\tnow", event_id="alias"),
        )

    assert checked == ["!p:now"]
    assert seen == ["!p:now"]


async def test_router_cmd_ignores_extension_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    router = EventRouter()

    @router.on_cmd("ping")
    def ping() -> None:
        pytest.fail("extension messages cannot be commands")

    bot.add_router(router)
    gateway = recording_gateway(bot)
    event = Event.model_validate({
        "id": "vendor",
        "self": {"platform": "test", "user_id": "bot"},
        "time": 1.0,
        "type": "message",
        "detail_type": "vendor.message",
        "sub_type": "",
    })

    async with bot:
        await bot.dispatch(gateway.connection, event)

    assert "Dispatch route failed" not in caplog.text
