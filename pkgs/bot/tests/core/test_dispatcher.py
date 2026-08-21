from asyncio import CancelledError, Event, QueueFull, TaskGroup, create_task, timeout
from contextvars import ContextVar
from datetime import timedelta
from typing import override

import pytest
from bot import Bot, Injected, PrivateMessageEvent
from bot.testing import RecordingGateway, private_message_event

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="missing")


def event(event_id: str) -> PrivateMessageEvent:
    return private_message_event(event_id, event_id=event_id)


@pytest.mark.parametrize(
    ("value", "exception"),
    [(True, TypeError), (1.0, TypeError), (0, ValueError), (-1, ValueError)],
    ids=["bool", "float", "zero", "negative"],
)
def test_max_dispatches_must_be_a_positive_integer(
    value: object,
    exception: type[Exception],
) -> None:
    with pytest.raises(exception):
        Bot(max_dispatches=value)  # ty: ignore[invalid-argument-type]


async def test_submission_requires_a_running_bot() -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)
    message = event("event")

    with pytest.raises(RuntimeError, match="Bot is not running"):
        bot.enqueue_event(gateway.connection, message)
    with pytest.raises(RuntimeError, match="Bot is not running"):
        await bot.dispatch(gateway.connection, message)

    await bot.start()
    await bot.close()

    with pytest.raises(RuntimeError, match="Bot is not running"):
        bot.enqueue_event(gateway.connection, message)


async def test_enqueue_preserves_fifo_order_and_context() -> None:
    bot = Bot(max_dispatches=1)
    gateway = RecordingGateway(bot)
    seen: list[tuple[str, str]] = []
    done = Event()

    @bot.on_msg()
    def handle(message: Injected[PrivateMessageEvent]) -> None:
        seen.append((message.id, _REQUEST_ID.get()))
        if len(seen) == 3:
            done.set()

    async with timeout(1):
        async with bot:
            token = _REQUEST_ID.set("captured")
            try:
                for event_id in ("first", "second", "third"):
                    bot.enqueue_event(gateway.connection, event(event_id))
            finally:
                _REQUEST_ID.reset(token)
            await done.wait()

    assert seen == [
        ("first", "captured"),
        ("second", "captured"),
        ("third", "captured"),
    ]


async def test_dispatch_respects_the_global_worker_limit() -> None:
    bot = Bot(max_dispatches=8, dispatch_timeout=None)
    gateway = RecordingGateway(bot)
    release = Event()
    limit_reached = Event()
    active = 0
    peak = 0

    @bot.on_msg()
    async def handle() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 8:
            limit_reached.set()
        try:
            await release.wait()
        finally:
            active -= 1

    async with timeout(1):
        async with bot, TaskGroup() as tasks:
            for index in range(9):
                tasks.create_task(bot.dispatch(gateway.connection, event(str(index))))
            await limit_reached.wait()
            assert peak == 8
            release.set()

    assert peak == 8


async def test_enqueue_raises_when_the_fixed_queue_is_full() -> None:
    bot = Bot(max_dispatches=1, dispatch_timeout=None)
    gateway = RecordingGateway(bot)
    started = Event()
    release = Event()

    @bot.on_msg()
    async def handle() -> None:
        started.set()
        await release.wait()

    async with timeout(1):
        async with bot:
            running = create_task(bot.dispatch(gateway.connection, event("running")))
            try:
                await started.wait()
                for index in range(64):
                    bot.enqueue_event(gateway.connection, event(f"queued-{index}"))
                with pytest.raises(QueueFull):
                    bot.enqueue_event(gateway.connection, event("overflow"))
            finally:
                release.set()
                await running


async def test_queued_event_uses_its_admission_deadline() -> None:
    bot = Bot(max_dispatches=1, dispatch_timeout=None)
    gateway = RecordingGateway(bot)
    started = Event()
    release = Event()
    expired_ran = False

    @bot.on_msg()
    async def handle(message: Injected[PrivateMessageEvent]) -> None:
        nonlocal expired_ran
        if message.id == "running":
            started.set()
            await release.wait()
        elif message.id == "expired":
            expired_ran = True

    async with timeout(1):
        async with bot:
            running = create_task(bot.dispatch(gateway.connection, event("running")))
            try:
                await started.wait()
                bot.dispatch_timeout = timedelta(0)
                bot.enqueue_event(gateway.connection, event("expired"))
                bot.dispatch_timeout = None
            finally:
                release.set()
                await running
            await bot.dispatch(gateway.connection, event("after"))

    assert not expired_ran


async def test_recursive_dispatch_is_rejected() -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)
    rejected = False

    @bot.on_msg(block=True)
    async def handle() -> None:
        nonlocal rejected
        with pytest.raises(RuntimeError, match="Recursive dispatch"):
            await bot.dispatch(gateway.connection, event("nested"))
        rejected = True

    async with bot:
        await bot.dispatch(gateway.connection, event("outer"))

    assert rejected


async def test_nested_dispatch_uses_the_destination_bot_container() -> None:
    first = Bot()
    second = Bot()
    first_gateway = RecordingGateway(first)
    second_gateway = RecordingGateway(second)
    seen: list[Bot] = []

    @second.on_msg(block=True)
    def inner(bot: Injected[Bot]) -> None:
        seen.append(bot)

    @first.on_msg(block=True)
    async def outer(bot: Injected[Bot]) -> None:
        seen.append(bot)
        await second.dispatch(second_gateway.connection, event("inner"))

    async with first, second:
        await first.dispatch(first_gateway.connection, event("outer"))

    assert seen == [first, second]


async def test_close_from_a_handler_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)
    seen: list[str] = []

    @bot.on_msg(block=True)
    async def handle(message: Injected[PrivateMessageEvent]) -> None:
        seen.append(message.id)
        if message.id == "close":
            await bot.close()

    async with bot:
        await bot.dispatch(gateway.connection, event("close"))
        await bot.dispatch(gateway.connection, event("after"))

    assert seen == ["close", "after"]
    assert any(
        "Bot cannot be closed from a dispatch handler" in message
        for message in caplog.messages
    )


async def test_close_cancels_running_and_queued_events() -> None:
    bot = Bot(max_dispatches=1, dispatch_timeout=None)
    gateway = RecordingGateway(bot)
    started = Event()
    cancelled = Event()
    handled: list[str] = []

    @bot.on_msg()
    async def handle(message: Injected[PrivateMessageEvent]) -> None:
        handled.append(message.id)
        started.set()
        try:
            await Event().wait()
        finally:
            cancelled.set()

    async with timeout(1):
        await bot.start()
        running = create_task(bot.dispatch(gateway.connection, event("running")))
        try:
            await started.wait()
            bot.enqueue_event(gateway.connection, event("queued"))
            await bot.close()
            await cancelled.wait()
            with pytest.raises(CancelledError):
                await running
        finally:
            if not running.done():
                running.cancel()
                with pytest.raises(CancelledError):
                    await running
            await bot.close()

    assert handled == ["running"]


async def test_close_cancels_handlers_before_closing_gateways() -> None:
    order: list[str] = []
    started = Event()

    class OrderedGateway(RecordingGateway):
        @override
        async def close(self) -> None:
            order.append("gateway closed")

    bot = Bot(dispatch_timeout=None)
    gateway = OrderedGateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg()
    async def handle() -> None:
        started.set()
        try:
            await Event().wait()
        finally:
            order.append("handler cancelled")

    async with timeout(1):
        await bot.start()
        dispatch = create_task(bot.dispatch(gateway.connection, event("event")))
        await started.wait()
        await bot.close()
        with pytest.raises(CancelledError):
            await dispatch

    assert order == ["handler cancelled", "gateway closed"]
