from __future__ import annotations

from asyncio import Event as AsyncEvent
from asyncio import Queue, wait_for
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import override
from zoneinfo import ZoneInfo

import pytest
from logbook import TestHandler as LogbookTestHandler

from lst_bot import (
    Bot,
    BotSelf,
    Connection,
    CronJob,
    CronScheduler,
    EventPayload,
    Injected,
    PrivateMessageEvent,
    State,
)
from tests.conftest import RecordingGateway


@dataclass(frozen=True)
class Service:
    value: str


class BlockingSleep:
    def __init__(self) -> None:
        self.started = AsyncEvent()
        self.delay: float | None = None

    async def __call__(self, delay: float) -> None:
        self.delay = delay
        self.started.set()
        await AsyncEvent().wait()


class AlternateGateway(RecordingGateway):
    @override
    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)


def make_private_event(
    *,
    self_id: str,
    event_id: str = "evt-1",
) -> PrivateMessageEvent:
    event = EventPayload.model_validate({
        "id": event_id,
        "self": {"platform": "test", "user_id": self_id},
        "time": 1.0,
        "type": "message",
        "detail_type": "private",
        "sub_type": "",
        "message_id": f"{event_id}-message",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "alt_message": "hello",
        "user_id": "42",
    }).root
    if not isinstance(event, PrivateMessageEvent):
        msg = "event factory must build private message events"
        raise TypeError(msg)
    return event


async def trigger_and_wait(job: CronJob) -> None:
    await job._trigger()
    if job._running is not None:
        await job._running


def install_scheduler(bot: Bot, scheduler: CronScheduler) -> None:
    bot._scheduler = scheduler


def utc_clock(tz: tzinfo) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC).astimezone(tz)


def test_on_cron_registers_validated_job_and_timezone() -> None:
    bot = Bot(scheduler_timezone=ZoneInfo("UTC"))

    @bot.on_cron("*/5 * * * *", name="five")
    def five() -> None:
        pass

    @bot.on_cron("0 9 * * *", timezone="Asia/Tokyo")
    def tokyo() -> None:
        pass

    five_job, tokyo_job = bot._scheduler.jobs

    assert five_job.name == "five"
    assert str(five_job.timezone) == "UTC"
    assert tokyo_job.name == "tokyo"
    assert str(tokyo_job.timezone) == "Asia/Tokyo"


def test_on_cron_rejects_invalid_cron_expression() -> None:
    bot = Bot()

    with pytest.raises(ValueError, match="Invalid cron expression"):
        bot.on_cron("0 0 31 2 *")(lambda: None)


async def test_scheduler_starts_with_bot_and_close_cancels_runner() -> None:
    bot = Bot(scheduler_timezone=ZoneInfo("UTC"))
    sleeper = BlockingSleep()
    install_scheduler(bot, CronScheduler(bot, clock=utc_clock, sleep=sleeper))

    @bot.on_cron("* * * * *", self_=None)
    def job() -> None:
        pass

    await bot.start()
    await wait_for(sleeper.started.wait(), timeout=1)

    registered = bot._scheduler.jobs[0]
    assert registered._runner is not None
    assert sleeper.delay == 60

    await bot.close()

    assert registered._runner is None


async def test_recent_account_job_uses_latest_dispatched_event_self() -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)
    seen: Queue[str] = Queue()

    @bot.on_cron("* * * * *")
    async def collect(connection: Injected[Connection]) -> None:
        await seen.put(connection.self_.user_id)

    registered = bot._scheduler.jobs[0]

    self_a = BotSelf(platform="test", user_id="bot-a")
    await bot.dispatch(
        gateway.connection_for(self_a),
        make_private_event(self_id="bot-a", event_id="evt-a"),
    )
    await registered._trigger()
    assert await wait_for(seen.get(), timeout=1) == "bot-a"

    self_b = BotSelf(platform="test", user_id="bot-b")
    await bot.dispatch(
        gateway.connection_for(self_b),
        make_private_event(self_id="bot-b", event_id="evt-b"),
    )
    await registered._trigger()
    assert await wait_for(seen.get(), timeout=1) == "bot-b"

    if registered._running is not None:
        await registered._running


async def test_recent_account_job_skips_without_recent_event_self() -> None:
    bot = Bot()
    called = False

    @bot.on_cron("* * * * *", name="recent")
    def collect() -> None:
        nonlocal called
        called = True

    registered = bot._scheduler.jobs[0]

    with LogbookTestHandler() as handler:
        await trigger_and_wait(registered)

    assert not called
    assert any(
        "no recent bot account exists" in record.message for record in handler.records
    )


async def test_none_self_job_runs_without_connection_target() -> None:
    bot = Bot()
    bot.container.add_instance(Service("ready"), provides=Service)
    seen: list[str] = []

    @bot.on_cron("* * * * *", self_=None)
    def collect(state: Injected[State], service: Injected[Service]) -> None:
        state["value"] = service.value
        seen.append(str(state["value"]))

    await trigger_and_wait(bot._scheduler.jobs[0])

    assert seen == ["ready"]


async def test_none_self_connection_injection_failure_is_logged() -> None:
    bot = Bot()

    @bot.on_cron("* * * * *", name="bad", self_=None)
    def bad(connection: Injected[Connection]) -> None:
        _ = connection

    with LogbookTestHandler() as handler:
        await trigger_and_wait(bot._scheduler.jobs[0])

    assert any(
        "Scheduled job failed" in record.message and "bad" in record.message
        for record in handler.records
    )


async def test_fixed_account_uses_single_gateway() -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)
    seen: Queue[str] = Queue()
    self_ = BotSelf(platform="test", user_id="fixed")

    @bot.on_cron("* * * * *", self_=self_)
    async def collect(connection: Injected[Connection]) -> None:
        await seen.put(connection.self_.user_id)

    await bot._scheduler.jobs[0]._trigger()

    assert await wait_for(seen.get(), timeout=1) == "fixed"


async def test_fixed_account_logs_multiple_gateway_ambiguity() -> None:
    bot = Bot()
    bot.add_gateway(RecordingGateway(bot))
    bot.add_gateway(AlternateGateway(bot))

    @bot.on_cron(
        "* * * * *",
        name="ambiguous",
        self_=BotSelf(platform="test", user_id="fixed"),
    )
    def collect(connection: Injected[Connection]) -> None:
        _ = connection

    with LogbookTestHandler() as handler:
        await trigger_and_wait(bot._scheduler.jobs[0])

    assert any(
        "failed to resolve target" in record.message for record in handler.records
    )


async def test_overlapping_job_skips_trigger() -> None:
    bot = Bot()
    started = AsyncEvent()
    release = AsyncEvent()

    @bot.on_cron("* * * * *", name="slow", self_=None)
    async def slow() -> None:
        started.set()
        await release.wait()

    registered = bot._scheduler.jobs[0]

    await registered._trigger()
    await wait_for(started.wait(), timeout=1)

    with LogbookTestHandler() as handler:
        await registered._trigger()

    assert any("still running" in record.message for record in handler.records)

    release.set()
    if registered._running is not None:
        await registered._running


async def test_handler_exception_does_not_block_later_triggers() -> None:
    bot = Bot()
    attempts = 0
    first = AsyncEvent()
    second = AsyncEvent()

    @bot.on_cron("* * * * *", name="flaky", self_=None)
    def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first.set()
            msg = "boom"
            raise RuntimeError(msg)
        second.set()

    registered = bot._scheduler.jobs[0]

    with LogbookTestHandler() as handler:
        await registered._trigger()
        await wait_for(first.wait(), timeout=1)
        if registered._running is not None:
            await registered._running
        await registered._trigger()
        await wait_for(second.wait(), timeout=1)

    assert attempts == 2
    assert any(
        "Scheduled job failed" in record.message and "flaky" in record.message
        for record in handler.records
    )


async def test_scheduler_close_cancels_running_handler() -> None:
    bot = Bot()
    started = AsyncEvent()
    cancelled = AsyncEvent()

    @bot.on_cron("* * * * *", self_=None)
    async def slow() -> None:
        try:
            started.set()
            await AsyncEvent().wait()
        finally:
            cancelled.set()

    registered = bot._scheduler.jobs[0]
    await registered._trigger()
    await wait_for(started.wait(), timeout=1)

    await bot._scheduler.close()

    assert cancelled.is_set()
    assert registered._running is None

    await bot.close()
