from __future__ import annotations

from asyncio import CancelledError, create_task
from asyncio import Event as AsyncEvent
from dataclasses import dataclass
from datetime import timedelta

import pytest
from bot import (
    ActionCall,
    ActionResponse,
    Bot,
    Cmd,
    Connection,
    EventRouter,
    GroupMessageEvent,
    Injected,
    Mention,
    Msg,
    Permission,
    PrivateMessageEvent,
    Reply,
    Retcode,
    ReturnAction,
    ReturnEffect,
)
from bot.testing import private_message_event as make_event
from bot.testing import recording_gateway
from diwire import Lifetime, Scope
from logbook import TestHandler as LogbookTestHandler


@dataclass(frozen=True)
class Service:
    value: str


@dataclass(frozen=True)
class RequestService:
    value: int


class Greeter:
    def reply(self, value: str) -> str:
        return f"pong {value}".strip()


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
            by_alias=True,
        ),
        "detail_type": "group",
        "group_id": "group-1",
        "sender": {"user_id": user_id, "role": sender_role},
    })


async def test_dispatch_runs_matching_cmd_with_injection() -> None:
    bot = Bot()
    bot.container.add_instance(Service("pong"), provides=Service)
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_cmd("ping", block=True)
    def ping(event: Injected[PrivateMessageEvent], service: Injected[Service]) -> None:
        seen.append(f"{event.user_id}:{service.value}")

    async with bot:
        await bot.dispatch(
            gateway.connection,
            make_event("/ping", user_id="42"),
        )

    assert seen == ["42:pong"]


async def test_connection_send_msg_builds_standard_action() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    response = await gateway.connection.send_msg("pong", group_id="20000")

    assert isinstance(response, ActionResponse)
    assert response.data == {"status": "ok"}
    action = gateway.actions[0].root
    assert action.action == "send_message"
    assert action.params.model_dump(mode="json", by_alias=True) == {
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

    @bot.on_msg(block=True)
    async def handle(connection: Injected[Connection]) -> None:
        await connection.send_msg("pong", user_id="42")

    with LogbookTestHandler() as handler:
        async with bot:
            results = await bot.dispatch(gateway.connection, make_event("ping"))

    exception = results[0].exception
    assert isinstance(exception, RuntimeError)
    assert str(exception) == "Action failed with retcode 10001: bad target"
    assert results[0].effects == []
    assert any(
        "Dispatch route" in record.message
        and "Action failed with retcode 10001: bad target" in record.message
        for record in handler.records
    )


async def test_dispatch_injects_connection_and_enforces_permission() -> None:
    bot = Bot(cmd_prefixes=("!",), admin_ids={"u1"})
    bot.container.add_instance(Greeter(), provides=Greeter)
    gateway = recording_gateway(bot)

    @bot.on_cmd("ping", permission=Permission.bot_admin(), block=True)
    async def handle(
        connection: Injected[Connection],
        cmd: Injected[Cmd],
        event: Injected[PrivateMessageEvent],
        greeter: Injected[Greeter],
    ) -> None:
        await connection.action(
            "send_message",
            user_id=event.user_id,
            message=Msg.t(greeter.reply(cmd.arg)),
        )

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            make_event("!ping hi", user_id="u1"),
        )

    assert len(results) == 1
    assert results[0].values == []
    action = gateway.actions[0].root
    assert action.action == "send_message"
    assert action.params.model_dump(mode="json", by_alias=True)["message"] == [
        {"type": "text", "data": {"text": "pong hi"}},
    ]


async def test_admin_permission_allows_bot_admin_or_sender_admin() -> None:
    bot = Bot(admin_ids={"root"})
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_cmd("secure", permission=Permission.admin(), block=True)
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
        results = await bot.dispatch(gateway.connection, make_event("ping"))

    assert results[0].values == ["pong"]
    assert len(results[0].effects) == 1
    effect = results[0].effects[0]
    assert isinstance(effect, ReturnEffect)
    assert effect.action.msg == Msg.t("pong")
    assert isinstance(effect.outcome, ActionResponse)
    assert effect.outcome.data == {"status": "ok"}
    assert gateway.actions[0].root.model_dump(mode="json", by_alias=True) == {
        "action": "send_message",
        "params": {
            "detail_type": "private",
            "message": [{"type": "text", "data": {"text": "pong"}}],
            "user_id": "42",
        },
    }


async def test_dispatch_executes_list_returns_in_order() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle() -> list[Msg]:
        return [Msg.from_input("one"), Msg.from_input("two")]

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("ping"))

    effects = results[0].effects
    assert all(isinstance(effect, ReturnEffect) for effect in effects)
    messages = [effect.action.msg for effect in effects]
    assert all(message is not None for message in messages)
    assert [message.text for message in messages if message is not None] == [
        "one",
        "two",
    ]
    assert [
        action.root.params.model_dump(mode="json", by_alias=True)["message"]
        for action in gateway.actions
    ] == [
        [{"type": "text", "data": {"text": "one"}}],
        [{"type": "text", "data": {"text": "two"}}],
    ]


async def test_dispatch_executes_action_returns() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle() -> list[ReturnAction | ActionCall]:
        return [
            ReturnAction.call(
                "send_message",
                {
                    "detail_type": "private",
                    "user_id": "42",
                    "message": "from wrapper",
                },
            ),
            ActionCall.model_validate({
                "action": "get_user_info",
                "params": {"user_id": "42"},
            }),
        ]

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("ping"))

    assert [action.root.action for action in gateway.actions] == [
        "send_message",
        "get_user_info",
    ]
    assert all(isinstance(effect, ReturnEffect) for effect in results[0].effects)
    assert [effect.action.kind for effect in results[0].effects] == ["call", "call"]


async def test_dispatch_injects_reply_and_mention_helpers() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle(
        reply: Injected[Reply],
        mention: Injected[Mention],
    ) -> list[ReturnAction]:
        return [mention(" look"), reply("done")]

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            make_event("ping", user_id="u1"),
        )

    assert [effect.action.kind for effect in results[0].effects] == [
        "message",
        "message",
    ]
    assert [
        action.root.params.model_dump(mode="json", by_alias=True)["message"]
        for action in gateway.actions
    ] == [
        [
            {"type": "mention", "data": {"user_id": "u1"}},
            {"type": "text", "data": {"text": " look"}},
        ],
        [
            {
                "type": "reply",
                "data": {"message_id": "evt-1-message", "user_id": "u1"},
            },
            {"type": "text", "data": {"text": "done"}},
        ],
    ]


async def test_dispatch_rejects_unsupported_return_values() -> None:
    cases = [
        (True, "bool"),
        ({"not": "supported"}, "dict"),
        (make_event("nested", event_id="nested"), "PrivateMessageEvent"),
    ]
    for value, type_name in cases:
        bot = Bot()
        gateway = recording_gateway(bot)

        bot.on_msg(block=True)(lambda value=value: value)

        async with bot:
            results = await bot.dispatch(
                gateway.connection,
                make_event(f"ping-{type_name}"),
            )

        assert len(results) == 1
        exception = results[0].exception
        assert isinstance(exception, TypeError)
        assert str(exception) == f"Unsupported handler return value: {type_name}"


async def test_dispatch_stops_batch_on_return_execution_error() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)

    @bot.on_msg(block=True)
    def handle() -> list[Msg | dict[str, str]]:
        return [
            Msg.from_input("first"),
            {"not": "supported"},
            Msg.from_input("never"),
        ]

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("ping"))

    assert [action.root.action for action in gateway.actions] == ["send_message"]
    assert len(results) == 1
    exception = results[0].exception
    assert isinstance(exception, TypeError)
    assert str(exception) == "Unsupported handler return value: dict"


async def test_dispatch_continues_after_failed_blocking_route() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_msg(priority=1, block=True)
    def fail() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    @bot.on_msg(priority=2, block=True)
    def recover() -> None:
        seen.append("recovered")

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["recovered"]
    assert len(results) == 2
    assert isinstance(results[0].exception, RuntimeError)
    assert results[1].exception is None


async def test_dispatch_treats_handler_timeout_error_as_a_route_error() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_msg(priority=1, block=True)
    def fail() -> None:
        msg = "business timeout"
        raise TimeoutError(msg)

    @bot.on_msg(priority=2, block=True)
    def recover() -> None:
        seen.append("recovered")

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["recovered"]
    assert len(results) == 2
    assert isinstance(results[0].exception, TimeoutError)
    assert str(results[0].exception) == "business timeout"
    assert results[1].exception is None


async def test_dispatch_records_check_and_dependency_exceptions() -> None:
    bot = Bot()
    router = EventRouter()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    def fail_rule() -> bool:
        msg = "rule failed"
        raise ValueError(msg)

    def fail_permission() -> bool:
        msg = "permission failed"
        raise PermissionError(msg)

    def fail_dependency() -> None:
        msg = "dependency failed"
        raise LookupError(msg)

    @router.on_msg(rule=fail_rule)
    def unreachable_rule() -> None:
        seen.append("rule")

    @router.on_msg(permission=fail_permission)
    def unreachable_permission() -> None:
        seen.append("permission")

    @router.on_msg(dependencies=[fail_dependency])
    def unreachable_dependency() -> None:
        seen.append("dependency")

    @router.on_msg(block=True)
    def recover() -> None:
        seen.append("recovered")

    bot.add_router(router)

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["recovered"]
    exceptions = [result.exception for result in results]
    assert isinstance(exceptions[0], ValueError)
    assert isinstance(exceptions[1], PermissionError)
    assert isinstance(exceptions[2], LookupError)
    assert exceptions[3] is None


async def test_dispatch_respects_priority_and_block() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_msg(priority=10)
    def late() -> None:
        seen.append("late")

    @bot.on_msg(priority=1, block=True)
    def early() -> None:
        seen.append("early")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("anything"))

    assert seen == ["early"]


async def test_dispatch_cmd_blocks_by_default() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_cmd("ping")
    def command() -> None:
        seen.append("command")

    @bot.on_msg()
    def message() -> None:
        seen.append("message")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("/ping"))

    assert seen == ["command"]


async def test_dispatch_cmd_can_opt_out_of_blocking() -> None:
    bot = Bot()
    gateway = recording_gateway(bot)
    seen: list[str] = []

    @bot.on_cmd("ping", block=False)
    def command() -> None:
        seen.append("command")

    @bot.on_msg()
    def message() -> None:
        seen.append("message")

    async with bot:
        await bot.dispatch(gateway.connection, make_event("/ping"))

    assert seen == ["command", "message"]


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


async def test_dispatch_timeout_cancels_route_and_future_dispatch_recovers() -> None:
    bot = Bot(dispatch_timeout=timedelta(seconds=0.01))
    gateway = recording_gateway(bot)
    slow_cancelled = AsyncEvent()
    seen: list[str] = []

    def is_slow(event: Injected[PrivateMessageEvent]) -> bool:
        return event.message.text == "slow"

    def is_fast(event: Injected[PrivateMessageEvent]) -> bool:
        return event.message.text == "fast"

    @bot.on_msg(rule=is_slow, block=True)
    async def slow() -> None:
        try:
            await AsyncEvent().wait()
        finally:
            slow_cancelled.set()

    @bot.on_msg(rule=is_fast, block=True)
    def fast() -> None:
        seen.append("fast")

    async with bot:
        results = await bot.dispatch(gateway.connection, make_event("slow"))
        fast_results = await bot.dispatch(
            gateway.connection,
            make_event("fast", event_id="evt-fast"),
        )

    assert slow_cancelled.is_set()
    assert len(results) == 1
    assert isinstance(results[0].exception, TimeoutError)
    assert seen == ["fast"]
    assert len(fast_results) == 1
    assert fast_results[0].exception is None


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

    async with bot:
        task = create_task(bot.dispatch(gateway.connection, make_event("slow")))
        await started.wait()
        task.cancel()
        with pytest.raises(CancelledError):
            await task
        assert not completed.is_set()
        release.set()
        await completed.wait()
