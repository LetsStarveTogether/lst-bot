from __future__ import annotations

from asyncio import CancelledError, create_task, sleep
from contextlib import suppress
from typing import override

from diwire import Container

from lst_bot import Bot, Injected
from tests.conftest import RecordingGateway


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


async def test_bot_run_keeps_lifecycle_until_cancelled() -> None:
    bot = Bot()
    seen: list[str] = []

    @bot.on_start
    def start_hook() -> None:
        seen.append("startup")

    @bot.on_close
    def close_hook() -> None:
        seen.append("close")

    task = create_task(bot.run())
    for _ in range(10):
        if seen:
            break
        await sleep(0)

    assert seen == ["startup"]

    task.cancel()
    with suppress(CancelledError):
        await task

    assert seen == ["startup", "close"]
