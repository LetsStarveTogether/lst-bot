from asyncio import CancelledError, Event, create_task, gather, timeout
from typing import override

import pytest
from bot import Bot, Gateway
from bot.testing import RecordingGateway, private_message_event


class CountingGateway(Gateway):
    starts = 0
    closes = 0

    @override
    async def start(self) -> None:
        self.starts += 1

    @override
    async def close(self) -> None:
        self.closes += 1


def test_bot_copies_admin_identity_mapping_as_immutable_sets() -> None:
    admin_ids = {"test": {"u1"}}
    bot = Bot(admin_ids=admin_ids)

    admin_ids["test"].add("u2")
    admin_ids["other"] = {"u3"}

    assert bot.admin_ids == {"test": frozenset({"u1"})}
    with pytest.raises(TypeError, match="user ID iterables"):
        Bot(admin_ids={"test": "root"})
    with pytest.raises(TypeError, match="must be strings"):
        Bot(admin_ids={"test": {42}})  # ty: ignore[invalid-argument-type]


def test_add_gateway_rejects_a_foreign_owner() -> None:
    bot = Bot()

    with pytest.raises(ValueError, match="another bot"):
        bot.add_gateway(RecordingGateway(Bot()))


async def test_bot_async_context_runs_lifecycle_and_closes_gateways() -> None:
    class LifecycleGateway(RecordingGateway):
        @override
        def __init__(self, bot: Bot, seen: list[str]) -> None:
            super().__init__(bot)
            self.seen = seen

        @override
        async def start(self) -> None:
            self.seen.append("start")

        @override
        async def close(self) -> None:
            self.seen.append("close")

    bot = Bot()
    seen: list[str] = []
    bot.add_gateway(LifecycleGateway(bot, seen))

    async with bot as active:
        assert active is bot
        seen.append("body")

    assert seen == ["start", "body", "close"]


async def test_bot_lifecycle_is_concurrently_idempotent_and_restartable() -> None:
    bot = Bot()
    gateway = CountingGateway(bot)
    bot.add_gateway(gateway)

    async with timeout(1):
        await gather(bot.start(), bot.start(), bot.start())
        await gather(bot.close(), bot.close(), bot.close())
        await bot.start()
        await bot.close()

    assert gateway.starts == 2
    assert gateway.closes == 2


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
        await entered.wait()
        close_requested.set()
        await bot.close()

    async with timeout(1):
        start_task = create_task(bot.start())
        close_task = create_task(close())
        try:
            await close_requested.wait()

            assert not close_task.done()

            release.set()
            await gather(start_task, close_task)
        finally:
            release.set()
            start_task.cancel()
            close_task.cancel()
            await gather(start_task, close_task, return_exceptions=True)
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
        try:
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
        finally:
            release.set()
            start_task.cancel()
            ready_task.cancel()
            await gather(start_task, ready_task, return_exceptions=True)
        await bot.close()


async def test_gateway_start_failure_runs_full_rollback() -> None:
    calls: list[str] = []

    class OrderedGateway(Gateway):
        def __init__(self, bot: Bot, name: str, *, fail_once: bool = False) -> None:
            super().__init__(bot)
            self.name = name
            self.fail_once = fail_once

        @override
        async def start(self) -> None:
            calls.append(f"start:{self.name}")
            if self.fail_once:
                self.fail_once = False
                msg = "gateway failed"
                raise RuntimeError(msg)

        @override
        async def close(self) -> None:
            calls.append(f"close:{self.name}")

    bot = Bot()
    bot.add_gateway(OrderedGateway(bot, "first"))
    bot.add_gateway(OrderedGateway(bot, "second", fail_once=True))

    with pytest.raises(RuntimeError, match="gateway failed"):
        await bot.start()

    expected = [
        "start:first",
        "start:second",
        "close:second",
        "close:first",
    ]
    assert calls == expected

    calls.clear()
    await bot.start()
    await bot.close()

    assert calls == expected


async def test_gateways_cannot_reenter_bot_lifecycle() -> None:
    async with timeout(1):

        class RecursiveStartGateway(Gateway):
            @override
            async def start(self) -> None:
                await self.bot.start()

        startup_bot = Bot()
        startup_bot.add_gateway(RecursiveStartGateway(startup_bot))

        with pytest.raises(RuntimeError, match="cannot be re-entered"):
            await startup_bot.start()

        class RecursiveChildStartGateway(Gateway):
            @override
            async def start(self) -> None:
                await create_task(self.bot.start())

        child_bot = Bot()
        child_bot.add_gateway(RecursiveChildStartGateway(child_bot))

        with pytest.raises(RuntimeError, match="cannot be re-entered"):
            await child_bot.start()

        class RecursiveCloseGateway(Gateway):
            closes = 0

            @override
            async def close(self) -> None:
                self.closes += 1
                if self.closes == 1:
                    await self.bot.close()

        closing_bot = Bot()
        closing_bot.add_gateway(RecursiveCloseGateway(closing_bot))

        await closing_bot.start()
        with pytest.raises(RuntimeError, match="cannot be re-entered"):
            await closing_bot.close()
        await closing_bot.close()


async def test_gateway_registration_freezes_after_startup_begins() -> None:
    bot = Bot()
    late_gateway = CountingGateway(bot)

    await bot.start()
    try:
        with pytest.raises(RuntimeError, match="after bot startup begins"):
            bot.add_gateway(late_gateway)
    finally:
        await bot.close()

    assert late_gateway.starts == 0
    assert late_gateway.closes == 0


async def test_failed_close_runs_every_cleanup_and_can_be_retried() -> None:
    calls: list[str] = []

    class ClosingGateway(Gateway):
        def __init__(self, bot: Bot, name: str, *, fail_once: bool = False) -> None:
            super().__init__(bot)
            self.name = name
            self.fail_once = fail_once

        @override
        async def close(self) -> None:
            calls.append(self.name)
            if self.fail_once:
                self.fail_once = False
                msg = "gateway close failed"
                raise RuntimeError(msg)

    bot = Bot()
    bot.add_gateway(ClosingGateway(bot, "later"))
    bot.add_gateway(ClosingGateway(bot, "flaky", fail_once=True))

    await bot.start()
    with pytest.raises(RuntimeError, match="gateway close failed"):
        await bot.close()

    assert calls == ["flaky", "later"]

    with pytest.raises(RuntimeError, match="shutdown is incomplete"):
        await bot.start()

    await bot.close()

    assert calls == ["flaky", "later", "flaky"]


async def test_failed_start_rollback_must_finish_before_retry() -> None:
    class FlakyGateway(Gateway):
        starts = 0
        closes = 0

        @override
        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                msg = "start failed"
                raise RuntimeError(msg)

        @override
        async def close(self) -> None:
            self.closes += 1
            if self.closes == 1:
                msg = "rollback failed"
                raise RuntimeError(msg)

    bot = Bot()
    gateway = FlakyGateway(bot)
    bot.add_gateway(gateway)

    with pytest.raises(ExceptionGroup, match="startup and rollback failed") as raised:
        await bot.start()
    assert [str(error) for error in raised.value.exceptions] == [
        "start failed",
        "rollback failed",
    ]
    with pytest.raises(RuntimeError, match="shutdown is incomplete"):
        await bot.start()

    await bot.close()
    await bot.start()
    await bot.close()

    assert gateway.starts == 2
    assert gateway.closes == 3


async def test_cancelled_start_rolls_back_and_can_restart() -> None:
    entered = Event()
    calls: list[str] = []

    class OrderedGateway(Gateway):
        def __init__(self, bot: Bot, name: str) -> None:
            super().__init__(bot)
            self.name = name
            self.starts = 0

        @override
        async def start(self) -> None:
            self.starts += 1
            calls.append(f"start:{self.name}")
            if self.name == "second" and self.starts == 1:
                entered.set()
                await Event().wait()

        @override
        async def close(self) -> None:
            calls.append(f"close:{self.name}")

    bot = Bot()
    bot.add_gateway(OrderedGateway(bot, "first"))
    bot.add_gateway(OrderedGateway(bot, "second"))

    async with timeout(1):
        start_task = create_task(bot.start())
        await entered.wait()
        start_task.cancel()
        with pytest.raises(CancelledError):
            await start_task

        expected = [
            "start:first",
            "start:second",
            "close:second",
            "close:first",
        ]
        assert calls == expected

        calls.clear()
        await bot.start()
        await bot.close()

        assert calls == expected


async def test_cancelled_close_retries_interrupted_dispatcher_cleanup() -> None:
    handler_started = Event()
    cleanup_started = Event()
    cleanup_cancelled = Event()
    cleanup_release = Event()
    calls: list[str] = []

    class ClosingGateway(Gateway):
        @override
        async def close(self) -> None:
            calls.append("gateway")

    bot = Bot()
    gateway = ClosingGateway(bot)
    bot.add_gateway(gateway)

    @bot.on_msg()
    async def blocking_handler() -> None:
        handler_started.set()
        try:
            await Event().wait()
        finally:
            cleanup_started.set()
            try:
                await cleanup_release.wait()
            except CancelledError:
                cleanup_cancelled.set()
                await cleanup_release.wait()

    try:
        async with timeout(1):
            await bot.start()
            event = private_message_event("block")
            dispatch_task = create_task(
                bot.dispatch(gateway.connection_for(event.self_), event)
            )
            await handler_started.wait()

            close_task = create_task(bot.close())
            await cleanup_started.wait()
            close_task.cancel()
            await cleanup_cancelled.wait()
            assert not close_task.done()
            cleanup_release.set()
            with pytest.raises(CancelledError):
                await close_task

            assert calls == ["gateway"]

            await bot.close()

            assert calls == ["gateway"]
            with pytest.raises(CancelledError):
                await dispatch_task
    finally:
        cleanup_release.set()
