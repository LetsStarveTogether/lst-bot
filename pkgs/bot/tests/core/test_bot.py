from __future__ import annotations

from asyncio import Event, create_task, gather, timeout
from typing import override

import pytest
from bot import Bot, Gateway, Injected
from bot.testing import RecordingGateway, private_message_event
from diwire import Container


class LifecycleService:
    value = "ready"


async def test_lifecycle_hooks_use_dependency_injection() -> None:
    bot = Bot()
    bot.container.add_instance(LifecycleService(), provides=LifecycleService)
    seen: list[str] = []

    @bot.on_start
    def start(service: Injected[LifecycleService]) -> None:
        seen.append(f"startup:{service.value}")

    @bot.on_close
    def stop(active_bot: Injected[Bot]) -> None:
        seen.append(f"close:{active_bot.cmd_prefixes[0]}")

    await bot.start()
    await bot.close()

    assert seen == ["startup:ready", "close:/"]


def test_bot_accepts_supplied_container() -> None:
    container = Container()
    bot = Bot(container=container)

    assert bot.container is container


async def test_bot_async_context_runs_lifecycle_and_closes_gateways() -> None:
    class ClosingGateway(RecordingGateway):
        @override
        def __init__(self, bot: Bot, seen: list[str]) -> None:
            super().__init__(bot)
            self.seen = seen

        @override
        async def close(self) -> None:
            self.seen.append("close")

    bot = Bot()
    seen: list[str] = []
    bot.add_gateway(ClosingGateway(bot, seen))

    @bot.on_start
    def start_hook() -> None:
        seen.append("startup")

    @bot.on_close
    def close() -> None:
        seen.append("close hook")

    async with bot as active:
        assert active is bot
        seen.append("body")

    assert seen == ["startup", "body", "close hook", "close"]


async def test_bot_lifecycle_is_concurrently_idempotent_and_restartable() -> None:
    class CountingGateway(Gateway):
        starts = 0
        closes = 0

        @override
        async def start(self) -> None:
            self.starts += 1

        @override
        async def close(self) -> None:
            self.closes += 1

    bot = Bot()
    gateway = CountingGateway(bot)
    bot.add_gateway(gateway)
    hooks = {"start": 0, "close": 0}

    @bot.on_start
    def start_hook() -> None:
        hooks["start"] += 1

    @bot.on_close
    def close_hook() -> None:
        hooks["close"] += 1

    async with timeout(1):
        await gather(bot.start(), bot.start(), bot.start())
        await gather(bot.close(), bot.close(), bot.close())
        await bot.start()
        await bot.close()

    assert gateway.starts == 2
    assert gateway.closes == 2
    assert hooks == {"start": 2, "close": 2}


async def test_bot_close_waits_for_start_and_leaves_bot_restartable() -> None:
    entered = Event()
    release = Event()
    calls: list[str] = []

    class BlockingGateway(RecordingGateway):
        @override
        async def start(self) -> None:
            calls.append("start")
            entered.set()
            await release.wait()

        @override
        async def close(self) -> None:
            calls.append("close")

    bot = Bot()
    bot.add_gateway(BlockingGateway(bot))
    close_requested = Event()

    async def close() -> None:
        close_requested.set()
        await bot.close()

    async with timeout(1):
        start_task = create_task(bot.start())
        await entered.wait()
        close_task = create_task(close())
        await close_requested.wait()

        assert not close_task.done()

        release.set()
        await gather(start_task, close_task)
        await bot.start()
        await bot.close()

    assert calls == ["start", "close", "start", "close"]


async def test_bot_rejects_submissions_until_start_is_complete() -> None:
    entered = Event()
    release = Event()
    ready = Event()

    class BlockingGateway(Gateway):
        @override
        async def start(self) -> None:
            entered.set()
            await release.wait()

    bot = Bot()
    gateway = BlockingGateway(bot)
    bot.add_gateway(gateway)

    async def observe_running() -> None:
        await bot.wait_until_running()
        ready.set()

    async with timeout(1):
        ready_task = create_task(observe_running())
        start_task = create_task(bot.start())
        await entered.wait()

        startup_event = private_message_event("during startup")
        with pytest.raises(RuntimeError, match="Bot is not running"):
            bot.enqueue_event(
                gateway.connection_for(startup_event.self_),
                startup_event,
            )
        assert not ready.is_set()

        release.set()
        await start_task
        await ready.wait()
        await ready_task
        await bot.close()


async def test_bot_start_failure_cleans_up_and_can_retry() -> None:
    class FlakyGateway(Gateway):
        starts = 0
        closes = 0

        @override
        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                msg = "startup failed"
                raise RuntimeError(msg)

        @override
        async def close(self) -> None:
            self.closes += 1

    bot = Bot()
    gateway = FlakyGateway(bot)
    bot.add_gateway(gateway)

    with pytest.raises(RuntimeError, match="startup failed"):
        await bot.start()

    assert gateway.closes == 1

    await bot.start()
    await bot.close()

    assert gateway.starts == 2
    assert gateway.closes == 2


async def test_bot_start_hook_failure_runs_full_rollback() -> None:
    calls: list[str] = []

    class OrderedGateway(Gateway):
        def __init__(self, bot: Bot, name: str) -> None:
            super().__init__(bot)
            self.name = name

        @override
        async def start(self) -> None:
            calls.append(f"start:{self.name}")

        @override
        async def close(self) -> None:
            calls.append(f"close:{self.name}")

    bot = Bot()
    bot.add_gateway(OrderedGateway(bot, "first"))
    bot.add_gateway(OrderedGateway(bot, "second"))
    fail = True

    @bot.on_start
    def start_hook() -> None:
        nonlocal fail
        calls.append("start hook")
        if fail:
            fail = False
            msg = "hook failed"
            raise RuntimeError(msg)

    @bot.on_close
    def close_hook() -> None:
        calls.append("close hook")

    with pytest.raises(RuntimeError, match="hook failed"):
        await bot.start()

    expected = [
        "start:first",
        "start:second",
        "start hook",
        "close hook",
        "close:second",
        "close:first",
    ]
    assert calls == expected

    calls.clear()
    await bot.start()
    await bot.close()

    assert calls == expected
