from asyncio import Event, Queue, Task, create_task, wait_for
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from bot import (
    Bot,
    BotSelf,
    Connection,
    Injected,
)
from bot.testing import RecordingGateway, recording_gateway


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
    pass


def use_scripted_time(bot: Bot) -> ScriptedSleep:
    sleep = ScriptedSleep()
    fixed_time = datetime(2026, 1, 1, tzinfo=UTC)
    bot.scheduler.clock = fixed_time.astimezone
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


async def test_cron_handler_cannot_close_its_bot() -> None:
    bot = Bot(scheduler_timezone=ZoneInfo("UTC"))
    sleep = use_scripted_time(bot)
    rejected = Event()
    release = Event()
    background: Task[None] | None = None

    async def close_later() -> None:
        await release.wait()
        await bot.close()

    @bot.on_cron("* * * * *", self_=None)
    async def shutdown() -> None:
        nonlocal background
        with pytest.raises(RuntimeError, match="scheduled handler"):
            await bot.close()
        with pytest.raises(RuntimeError, match="scheduled handler"):
            await create_task(bot.scheduler.close())
        assert bot.scheduler._running  # ruff: ignore[private-member-access]
        with pytest.raises(RuntimeError, match="cannot close themselves"):
            await bot.scheduler.jobs[0].close()
        background = create_task(close_later())
        rejected.set()

    await bot.start()
    try:
        await sleep.advance()
        await wait_for(rejected.wait(), timeout=1)
        handler_task = bot.scheduler.jobs[0]._running  # ruff: ignore[private-member-access]
        assert handler_task is not None
        await wait_for(handler_task, timeout=1)
        assert background is not None
        release.set()
        await wait_for(background, timeout=1)
    finally:
        release.set()
        await bot.close()


@pytest.mark.parametrize(
    ("now", "expr", "expected_delay"),
    [
        (
            datetime(2026, 3, 8, 1, 59, tzinfo=ZoneInfo("America/New_York")),
            "0 3 * * *",
            60,
        ),
        (
            datetime(2026, 11, 1, 0, 59, tzinfo=ZoneInfo("America/New_York")),
            "0 2 * * *",
            7260,
        ),
    ],
)
async def test_cron_delay_uses_absolute_time_across_dst(
    now: datetime,
    expr: str,
    expected_delay: int,
) -> None:
    bot = Bot(scheduler_timezone=now.tzinfo)
    sleep = ScriptedSleep()
    bot.scheduler.clock = lambda _: now
    bot.scheduler.sleep = sleep
    bot.on_cron(expr, self_=None)(lambda: None)

    await bot.start()
    delay, _ = await sleep.next_call()
    await bot.close()

    assert delay == expected_delay


async def test_none_target_job_injects_service() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    bot.container.add_instance(Service("ready"), provides=Service)
    seen: Queue[str] = Queue()

    @bot.on_cron("* * * * *")
    async def collect(service: Injected[Service]) -> None:
        await seen.put(service.value)

    async with bot:
        await sleep.advance()
        assert await wait_for(seen.get(), timeout=1) == "ready"


async def test_none_target_connection_injection_failure_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)

    @bot.on_cron("* * * * *", name="bad", self_=None)
    def bad(connection: Injected[Connection]) -> None:
        _ = connection

    async with bot:
        await sleep.advance()
        await sleep.next_call()

    assert any(
        "Scheduled job failed" in message and "bad" in message
        for message in caplog.messages
    )


async def test_fixed_account_uses_the_registered_gateway() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    recording_gateway(bot)
    selected_gateway = AlternateGateway(bot)
    bot.add_gateway(selected_gateway)
    seen: Queue[Connection] = Queue()
    self_ = BotSelf(platform="test", user_id="fixed")

    @bot.on_cron("* * * * *", self_=self_, gateway=AlternateGateway)
    async def collect(connection: Injected[Connection]) -> None:
        await seen.put(connection)

    async with bot:
        await sleep.advance()
        connection = await wait_for(seen.get(), timeout=1)
        assert connection.gateway is selected_gateway
        assert connection.self_ == self_


async def test_fixed_account_logs_gateway_ambiguity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    recording_gateway(bot)
    bot.add_gateway(AlternateGateway(bot))

    @bot.on_cron(
        "* * * * *",
        name="ambiguous",
        self_=BotSelf(platform="test", user_id="fixed"),
    )
    def collect(connection: Injected[Connection]) -> None:
        _ = connection

    async with bot:
        await sleep.advance()
        await sleep.next_call()

    assert any("failed to resolve target" in message for message in caplog.messages)


async def test_overlapping_tick_is_skipped() -> None:
    bot = Bot()
    sleep = use_scripted_time(bot)
    started = Event()
    release = Event()
    attempts = 0

    @bot.on_cron("* * * * *", name="slow", self_=None)
    async def slow() -> None:
        nonlocal attempts
        attempts += 1
        started.set()
        await release.wait()

    async with bot:
        await sleep.advance()
        await wait_for(started.wait(), timeout=1)
        await sleep.advance()
        await sleep.next_call()
        assert attempts == 1
        release.set()


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

    async with bot:
        await sleep.advance()
        assert await wait_for(attempts.get(), timeout=1) == 1
        await sleep.advance()
        assert await wait_for(attempts.get(), timeout=1) == 2
