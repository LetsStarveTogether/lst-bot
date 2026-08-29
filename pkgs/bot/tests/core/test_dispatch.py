from asyncio import CancelledError, TaskGroup, timeout
from asyncio import Event as AsyncEvent
from collections.abc import Generator
from dataclasses import dataclass
from datetime import timedelta

import pytest
from bot import (
    ActionCall,
    ActionResponse,
    Bot,
    Cmd,
    EventRouter,
    GroupMessageEvent,
    Injected,
    Msg,
    PrivateMessageEvent,
    Retcode,
    admin_permission,
)
from bot.testing import private_message_event as make_event
from bot.testing import recording_gateway
from diwire import Lifetime, Scope


@dataclass(frozen=True)
class RequestService:
    value: int


def group_message_event(
    text: str,
    *,
    user_id: str,
    event_id: str,
    sender_role: str,
) -> GroupMessageEvent:
    return GroupMessageEvent.model_validate({
        **make_event(text, user_id=user_id, event_id=event_id).model_dump(
            mode="json",
        ),
        "detail_type": "group",
        "group_id": "group-1",
        "sender": {"user_id": user_id, "role": sender_role},
    })


async def test_connection_send_msg_builds_standard_action() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    response = await gateway.connection.send_msg("pong", group_id="20000")

    assert isinstance(response, ActionResponse)
    assert response.data == {"status": "ok"}
    (action,) = gateway.actions
    assert action.action == "send_message"
    assert action.params.model_dump(mode="json") == {
        "detail_type": "group",
        "group_id": "20000",
        "message": [{"type": "text", "data": {"text": "pong"}}],
    }


async def test_connection_action_failed_response_raises() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    gateway.responses["send_message"] = ActionResponse.failed(
        Retcode.BAD_REQUEST,
        "bad target",
    )

    with pytest.raises(
        RuntimeError,
        match="Action failed with retcode 10001: bad target",
    ):
        await gateway.connection.send_msg("pong", user_id="42")

    assert [action.action for action in gateway.actions] == ["send_message"]


async def test_admin_permission_allows_bot_admin_or_sender_admin() -> None:
    bot = Bot(admin_ids={"test": {"root"}, "qq": {"42"}})
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_cmd("secure", admin_permission, block=True)
    def secure(cmd: Injected[Cmd]) -> None:
        seen.append(cmd.arg)

    async with bot:
        for event in [
            make_event("/secure bot", user_id="root", event_id="bot-admin"),
            group_message_event(
                "/secure group",
                user_id="group-admin",
                event_id="group-admin",
                sender_role="admin",
            ),
            group_message_event(
                "/secure owner",
                user_id="group-owner",
                event_id="group-owner",
                sender_role="owner",
            ),
            group_message_event(
                "/secure member",
                user_id="member",
                event_id="member",
                sender_role="member",
            ),
            make_event("/secure foreign", user_id="42", event_id="foreign"),
        ]:
            await bot.dispatch(gateway.connection, event)

    assert seen == ["bot", "group", "owner"]


async def test_dispatch_auto_replies_string_return() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle() -> str:
        return "pong"

    async with bot:
        await bot.dispatch(gateway.connection, make_event("ping"))

    (action,) = gateway.actions
    assert action.model_dump(mode="json") == {
        "action": "send_message",
        "params": {
            "detail_type": "private",
            "message": [{"type": "text", "data": {"text": "pong"}}],
            "user_id": "42",
        },
    }


async def test_dispatch_uses_a_stable_route_snapshot() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_msg()
    def old() -> None:
        seen.append("old")
        if len(seen) == 1:

            @bot.on_msg(priority=0)
            def new() -> None:
                seen.append("new")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("first"))
        await bot.dispatch(gateway.connection, make_event("second"))

    assert seen == ["old", "new", "old"]


async def test_dispatch_executes_batch_returns_in_order() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle() -> list[Msg | ActionCall]:
        return [
            Msg.from_input("first"),
            ActionCall.model_validate({
                "action": "send_message",
                "params": {
                    "detail_type": "private",
                    "user_id": "42",
                    "message": "from wrapper",
                },
            }),
            ActionCall.model_validate({
                "action": "get_user_info",
                "params": {"user_id": "42"},
            }),
        ]

    async with bot:
        await bot.dispatch(gateway.connection, make_event("ping"))

    assert [action.action for action in gateway.actions] == [
        "send_message",
        "send_message",
        "get_user_info",
    ]
    assert gateway.actions[0].params.model_dump(mode="json")["message"] == [
        {"type": "text", "data": {"text": "first"}},
    ]


@pytest.mark.parametrize(
    "unsupported",
    [
        pytest.param({"not": "supported"}, id="mapping"),
        pytest.param([Msg.from_input("nested")], id="nested-batch"),
    ],
)
async def test_dispatch_stops_batch_on_return_execution_error(
    unsupported: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle() -> list[object]:
        return [
            Msg.from_input("first"),
            unsupported,
            Msg.from_input("never"),
        ]

    async with bot:
        await bot.dispatch(gateway.connection, make_event("ping"))

    assert [action.action for action in gateway.actions] == ["send_message"]
    assert any(
        f"Unsupported handler return value: {type(unsupported).__name__}" in message
        for message in caplog.messages
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("boom"), id="runtime-error"),
        pytest.param(TimeoutError("business timeout"), id="handler-timeout"),
    ],
)
async def test_dispatch_continues_after_failed_blocking_route(
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_msg(priority=1, block=True)
    def fail() -> None:
        raise error

    @bot.on_msg(priority=2, block=True)
    def recover() -> None:
        seen.append("recovered")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["recovered"]
    assert any(
        type(error).__name__ in message and str(error) in message
        for message in caplog.messages
    )


async def test_dispatch_records_predicate_exceptions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    def fail_predicate() -> bool:
        msg = "predicate failed"
        raise ValueError(msg)

    @bot.on_msg(fail_predicate)
    def unreachable() -> None:
        seen.append("unreachable")

    @bot.on_msg(block=True)
    def recover() -> None:
        seen.append("recovered")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["recovered"]
    assert [
        message.rsplit("(", 1)[-1].rstrip(")")
        for message in caplog.messages
        if "Dispatch route failed" in message
    ] == ["ValueError: predicate failed"]


async def test_dispatch_respects_priority_and_block() -> None:
    bot = Bot()
    router = EventRouter()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_msg(priority=10)
    def late() -> None:
        seen.append("late")

    @router.on_msg(priority=1, block=True)
    def early() -> None:
        seen.append("early")

    bot.add_router(router)

    async with bot:
        await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["early"]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        pytest.param(None, ["command"], id="default"),
        pytest.param(False, ["command", "message"], id="non-blocking"),
    ],
)
async def test_dispatch_cmd_blocking(
    block: bool | None,
    expected: list[str],
) -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    register = bot.on_cmd("ping") if block is None else bot.on_cmd("ping", block=block)

    @register
    def command() -> None:
        seen.append("command")

    @bot.on_msg()
    def message() -> None:
        seen.append("message")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("/ping"))

    assert seen == expected


async def test_dispatch_uses_request_scoped_container_dependencies() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    created: list[RequestService] = []
    seen: list[int] = []

    def build_service() -> RequestService:
        service = RequestService(len(created) + 1)
        created.append(service)
        return service

    bot.container.add_factory(
        build_service,
        provides=RequestService,
        scope=Scope.REQUEST,
        lifetime=Lifetime.SCOPED,
    )

    @bot.on_msg(block=True)
    def handle(service: Injected[RequestService]) -> None:
        seen.append(service.value)

    contract_count = len(
        bot.container._injected_scope_contracts,  # ruff: ignore[private-member-access] - diwire wrapper regression
    )
    async with bot:
        await bot.dispatch(
            gateway.connection,
            make_event("first", event_id="evt-first"),
        )
        await bot.dispatch(
            gateway.connection,
            make_event("second", event_id="evt-second"),
        )

    assert seen == [1, 2]
    assert (
        len(
            bot.container._injected_scope_contracts,  # ruff: ignore[private-member-access] - diwire wrapper regression
        )
        == contract_count
    )


async def test_restart_recreates_app_scoped_dependencies() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    created: list[RequestService] = []
    closed: list[RequestService] = []
    seen: list[int] = []

    def build_service() -> Generator[RequestService]:
        service = RequestService(len(created) + 1)
        created.append(service)
        try:
            yield service
        finally:
            closed.append(service)

    bot.container.add_generator(
        build_service,
        provides=RequestService,
        scope=Scope.APP,
        lifetime=Lifetime.SCOPED,
    )

    @bot.on_msg(block=True)
    def handle(service: Injected[RequestService]) -> None:
        seen.append(service.value)

    for index in (1, 2):
        async with bot:
            await bot.dispatch(gateway.connection, make_event(str(index)))
        assert len(closed) == index

    assert seen == [1, 2]


async def test_dispatch_timeout_cancels_route_and_future_dispatch_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot(dispatch_timeout=timedelta(seconds=0.01))
    gateway = recording_gateway(bot)
    slow_cancelled = AsyncEvent()
    seen: list[str] = []

    def is_slow(event: Injected[PrivateMessageEvent]) -> bool:
        return event.message.text == "slow"

    def is_fast(event: Injected[PrivateMessageEvent]) -> bool:
        return event.message.text == "fast"

    @bot.on_msg(is_slow, block=True)
    async def slow() -> None:
        try:
            await AsyncEvent().wait()
        finally:
            slow_cancelled.set()

    @bot.on_msg(is_fast, block=True)
    def fast() -> None:
        seen.append("fast")

    async with timeout(1):
        async with bot:
            await bot.dispatch(gateway.connection, make_event("slow"))
            await bot.dispatch(
                gateway.connection,
                make_event("fast", event_id="evt-fast"),
            )

    assert slow_cancelled.is_set()
    assert seen == ["fast"]
    assert sum("timed out" in message for message in caplog.messages) == 1


async def test_dispatch_external_cancellation_propagates() -> None:
    bot = Bot(dispatch_timeout=None)
    gateway = recording_gateway(bot)
    started = AsyncEvent()
    release = AsyncEvent()
    completed = AsyncEvent()

    @bot.on_msg(block=True)
    async def slow() -> None:
        started.set()
        await release.wait()
        completed.set()

    async with timeout(1), bot, TaskGroup() as tasks:
        task = tasks.create_task(bot.dispatch(gateway.connection, make_event("slow")))
        try:
            await started.wait()
            task.cancel()
            with pytest.raises(CancelledError):
                await task
            assert not completed.is_set()
            release.set()
            await completed.wait()
        finally:
            release.set()
