from __future__ import annotations

from asyncio import Event, Queue, wait_for
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import override
from zoneinfo import ZoneInfo

import pytest
from bot import (
    Bot,
    BotSelf,
    Connection,
    EventPayload,
    Injected,
    PrivateMessageEvent,
    State,
)
from bot.testing import RecordingGateway
from logbook import TestHandler as LogbookTestHandler


@dataclass(frozen=True)
class Service:
    value: str


class ScriptedSleep:
    def __init__(self) -> None:
        self.calls: Queue[tuple[float, Event]] = Queue()

    async def __call__(self, delay: float) -> None:
        release = Event()
        await self.calls.put((delay, release))
        await release.wait()

    async def advance(self) -> float:
        delay, release = await self.next_call()
        release.set()
        return delay

    async def next_call(self) -> tuple[float, Event]:
        return await wait_for(self.calls.get(), timeout=1)


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
    assert isinstance(event, PrivateMessageEvent)
    return event


def utc_clock(timezone: tzinfo) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC).astimezone(timezone)


def use_scripted_time(bot: Bot) -> ScriptedSleep:
    sleep = ScriptedSleep()
    bot.scheduler.clock = utc_clock
    bot.scheduler.sleep = sleep
    return sleep


def test_on_cron_registers_validated_jobs_in_the_public_view() -> None:
    bot = Bot(scheduler_timezone=ZoneInfo("UTC"))

    @bot.on_cron("*/5 * * * *", name="five")
    def five() -> None:
        pass

    @bot.on_cron("0 9 * * *", timezone="Asia/Tokyo")
    def tokyo() -> None:
        pass

    five_job, tokyo_job = bot.scheduler.jobs

    assert isinstance(bot.scheduler.jobs, tuple)
    assert five_job.name == "five"
    assert str(five_job.timezone) == "UTC"
    assert tokyo_job.name == "tokyo"
    assert str(tokyo_job.timezone) == "Asia/Tokyo"


def test_on_cron_rejects_invalid_cron_expression() -> None:
    bot = Bot()

    with pytest.raises(ValueError, match="Invalid cron expression"):
        bot.on_cron("0 0 31 2 *")(lambda: None)


async def test_bot_lifecycle_starts_ticks_and_cancels_the_running_handler() -> None:
    bot = Bot(scheduler_timezone=ZoneInfo("UTC"))
    sleep = use_scripted_time(bot)
    started = Event()
    cancelled = Event()

    @bot.on_cron("* * * * *", self_=None)
    async def job() -> None:
        try:
            started.set()
            await Event().wait()
        finally:
            cancelled.set()

    async with bot:
        assert await sleep.advance() == 60
        await wait_for(started.wait(), timeout=1)

    assert cancelled.is_set()


async def test_recent_account_job_uses_the_latest_dispatched_event() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)
    seen: Queue[str] = Queue()

    @bot.on_cron("* * * * *")
    async def collect(connection: Injected[Connection]) -> None:
        await seen.put(connection.self_.user_id)

    async with bot:
        self_a = BotSelf(platform="test", user_id="bot-a")
        await bot.dispatch(
            gateway.connection_for(self_a),
            make_private_event(self_id="bot-a", event_id="evt-a"),
        )
        await sleep.advance()
        assert await wait_for(seen.get(), timeout=1) == "bot-a"

        self_b = BotSelf(platform="test", user_id="bot-b")
        await bot.dispatch(
            gateway.connection_for(self_b),
            make_private_event(self_id="bot-b", event_id="evt-b"),
        )
        await sleep.advance()
        assert await wait_for(seen.get(), timeout=1) == "bot-b"


async def test_recent_account_job_skips_without_a_recent_event() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    called = Event()

    @bot.on_cron("* * * * *", name="recent")
    def collect() -> None:
        called.set()

    with LogbookTestHandler() as handler:
        async with bot:
            await sleep.advance()
            await sleep.next_call()

    assert not called.is_set()
    assert any(
        "no recent bot account exists" in record.message for record in handler.records
    )


async def test_none_target_job_has_fresh_state_and_dependencies() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    bot.container.add_instance(Service("ready"), provides=Service)
    seen: Queue[tuple[str, bool]] = Queue()

    @bot.on_cron("* * * * *", self_=None)
    async def collect(state: Injected[State], service: Injected[Service]) -> None:
        fresh = "value" not in state
        state["value"] = service.value
        await seen.put((str(state["value"]), fresh))

    async with bot:
        await sleep.advance()
        await sleep.advance()
        assert await wait_for(seen.get(), timeout=1) == ("ready", True)
        assert await wait_for(seen.get(), timeout=1) == ("ready", True)


async def test_none_target_connection_injection_failure_is_logged() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)

    @bot.on_cron("* * * * *", name="bad", self_=None)
    def bad(connection: Injected[Connection]) -> None:
        _ = connection

    with LogbookTestHandler() as handler:
        async with bot:
            await sleep.advance()
            await sleep.next_call()

    assert any(
        "Scheduled job failed" in record.message and "bad" in record.message
        for record in handler.records
    )


async def test_fixed_account_uses_the_registered_gateway() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)
    seen: Queue[str] = Queue()
    self_ = BotSelf(platform="test", user_id="fixed")

    @bot.on_cron("* * * * *", self_=self_)
    async def collect(connection: Injected[Connection]) -> None:
        await seen.put(connection.self_.user_id)

    async with bot:
        await sleep.advance()
        assert await wait_for(seen.get(), timeout=1) == "fixed"


async def test_fixed_account_logs_gateway_ambiguity() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
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
        async with bot:
            await sleep.advance()
            await sleep.next_call()

    assert any(
        "failed to resolve target" in record.message for record in handler.records
    )


async def test_overlapping_tick_is_skipped() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    started = Event()
    release = Event()

    @bot.on_cron("* * * * *", name="slow", self_=None)
    async def slow() -> None:
        started.set()
        await release.wait()

    with LogbookTestHandler() as handler:
        async with bot:
            await sleep.advance()
            await wait_for(started.wait(), timeout=1)
            await sleep.advance()
            await sleep.next_call()
            release.set()

    assert any("still running" in record.message for record in handler.records)


async def test_handler_failure_does_not_block_later_ticks() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    attempts: Queue[int] = Queue()
    count = 0

    @bot.on_cron("* * * * *", name="flaky", self_=None)
    def flaky() -> None:
        nonlocal count
        count += 1
        attempts.put_nowait(count)
        if count == 1:
            msg = "boom"
            raise RuntimeError(msg)

    with LogbookTestHandler() as handler:
        async with bot:
            await sleep.advance()
            assert await wait_for(attempts.get(), timeout=1) == 1
            await sleep.advance()
            assert await wait_for(attempts.get(), timeout=1) == 2

    assert any(
        "Scheduled job failed" in record.message and "flaky" in record.message
        for record in handler.records
    )
