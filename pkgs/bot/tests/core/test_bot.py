from asyncio import CancelledError, Event, Task, create_task, gather, sleep, timeout
from types import MappingProxyType
from typing import override

import pytest
from bot import Bot, Gateway
from bot.core import bot as bot_module
from bot.testing import RecordingGateway, private_message_event
from pydantic import ValidationError


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
    admin_ids = {
        "generator": iter(("u1",)),
        "list": ["u1"],
        "set": {"u1"},
    }
    expected = dict.fromkeys(admin_ids, frozenset({"u1"}))
    bot = Bot(admin_ids=MappingProxyType(admin_ids))

    admin_ids.clear()

    assert bot.admin_ids == expected
    for invalid in (
        False,
        {"test": "secret-admin-id"},
        {"test": {42}},
        {42: {"u1"}},
    ):
        with pytest.raises(ValidationError) as error:
            Bot(admin_ids=invalid)  # ty: ignore[invalid-argument-type]
        assert "secret-admin-id" not in str(error.value)


def test_add_gateway_is_identity_idempotent_and_rejects_a_foreign_owner() -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)

    bot.add_gateway(gateway)
    bot.add_gateway(gateway)

    assert bot.resolve_gateway() is gateway
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
            def __init__(self, bot: Bot, target: Bot | None = None) -> None:
                super().__init__(bot)
                self.target = target or bot

            @override
            async def start(self) -> None:
                await self.target.start()

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

        first = Bot()
        second = Bot()
        first.add_gateway(RecursiveStartGateway(first, second))
        second.add_gateway(RecursiveStartGateway(second, first))

        with pytest.raises(RuntimeError, match="cannot be re-entered"):
            await first.start()

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


async def test_stale_lifecycle_context_waits_for_the_current_operation() -> None:
    trigger = Event()
    attempted = Event()
    close_entered = Event()
    close_release = Event()
    starts = 0
    background: Task[None] | None = None

    async def start_later(bot: Bot) -> None:
        await trigger.wait()
        attempted.set()
        await bot.start()

    class DelayedGateway(Gateway):
        @override
        async def start(self) -> None:
            nonlocal background, starts
            starts += 1
            if background is None:
                background = create_task(start_later(self.bot))

        @override
        async def close(self) -> None:
            close_entered.set()
            await close_release.wait()

    bot = Bot()
    bot.add_gateway(DelayedGateway(bot))

    async with timeout(1):
        await bot.start()
        task = background
        assert task is not None

        async def coordinate() -> None:
            await close_entered.wait()
            trigger.set()
            await attempted.wait()
            try:
                await sleep(0)
                assert not task.done()
            finally:
                close_release.set()

        coordinator = create_task(coordinate())
        await bot.close()
        await coordinator
        await task
        assert starts == 2
        await bot.close()


async def test_background_restarts_prune_stale_lifecycle_owners() -> None:
    depths: list[int] = []

    async def restart(bot: Bot) -> None:
        await bot.close()
        await bot.start()

    class RestartingGateway(Gateway):
        starts = 0
        restart_task: Task[None] | None = None

        @override
        async def start(self) -> None:
            self.starts += 1
            depths.append(len(bot_module._CURRENT_LIFECYCLE.get()))  # ruff: ignore[private-member-access]
            self.restart_task = (
                create_task(restart(self.bot)) if self.starts < 3 else None
            )

        @override
        async def close(self) -> None:
            depths.append(len(bot_module._CURRENT_LIFECYCLE.get()))  # ruff: ignore[private-member-access]

    bot = Bot()
    gateway = RestartingGateway(bot)
    bot.add_gateway(gateway)
    async with timeout(1):
        await bot.start()
        try:
            for _ in range(2):
                task = gateway.restart_task
                assert task is not None
                await task
            assert depths == [1] * 5
        finally:
            await bot.close()


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


async def test_cancelled_close_finishes_dispatcher_cleanup() -> None:
    handler_started = Event()
    cleanup_started = Event()
    cleanup_release = Event()
    cleanup_finished = Event()
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
            await cleanup_release.wait()
            cleanup_finished.set()

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
            await sleep(0)
            close_task.cancel()
            await sleep(0)
            assert not close_task.done()
            cleanup_release.set()
            with pytest.raises(CancelledError):
                await close_task

            assert cleanup_finished.is_set()
            assert calls == ["gateway"]

            await bot.close()

            assert calls == ["gateway"]
            with pytest.raises(CancelledError):
                await dispatch_task
    finally:
        cleanup_release.set()


async def test_repeated_close_cancellation_stays_cancelled() -> None:
    entered = [Event(), Event()]
    release = [Event(), Event()]

    class BlockingGateway(Gateway):
        def __init__(self, bot: Bot, index: int) -> None:
            super().__init__(bot)
            self.index = index
            self.closes = 0

        @override
        async def close(self) -> None:
            self.closes += 1
            if self.closes == 1:
                entered[self.index].set()
                await release[self.index].wait()

    bot = Bot()
    bot.add_gateway(BlockingGateway(bot, 0))
    bot.add_gateway(BlockingGateway(bot, 1))

    async with timeout(1):
        await bot.start()
        closing = create_task(bot.close())
        await entered[1].wait()
        closing.cancel()
        await sleep(0)
        assert not closing.done()
        release[1].set()
        await entered[0].wait()
        closing.cancel()
        await sleep(0)
        assert not closing.done()
        release[0].set()

        with pytest.raises(CancelledError):
            await closing
        assert closing.cancelled()
        await bot.close()
