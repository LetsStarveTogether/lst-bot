from asyncio import CancelledError, Task, create_task, current_task, gather
from asyncio import sleep as async_sleep
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from logging import getLogger
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from croniter import croniter
from diwire import Scope

from bot._tasks import await_cleanup
from bot.gateways import Connection, Gateway
from bot.protocol.common import BotSelf

from .di import InjectionContext, call_with_injection, inject

if TYPE_CHECKING:
    from .bot import Bot


logger = getLogger(__name__)

type Sleep = Callable[[float], Awaitable[object]]
type Clock = Callable[[tzinfo], datetime]

CURRENT_SCHEDULER_BOT: ContextVar[tuple[object, Task[None]] | None] = ContextVar(
    "bot_current_scheduler",
    default=None,
)


def _scheduled_handler_is_active_for(bot: Bot) -> bool:
    owner = CURRENT_SCHEDULER_BOT.get()
    return owner is not None and owner[0] is bot and not owner[1].done()


def _raise_errors(message: str, errors: list[BaseException]) -> None:
    cancelled = next(
        (error for error in reversed(errors) if isinstance(error, CancelledError)),
        None,
    )
    errors = [error for error in errors if not isinstance(error, CancelledError)]
    if cancelled is not None:
        errors.insert(0, cancelled)
    if len(errors) == 1:
        raise errors[0] from None
    if errors:
        raise BaseExceptionGroup(message, errors) from None


@dataclass(slots=True)
class CronJob:
    bot: Bot
    expr: str
    handler: Callable
    name: str
    timezone: tzinfo
    self_: BotSelf | None
    gateway_type: type[Gateway] | None
    clock: Clock
    sleep: Sleep
    _runner: Task[None] | None = None
    _running: Task[None] | None = None
    _closing: bool = False

    def __post_init__(self) -> None:
        self.handler = inject(self.handler)

    def __str__(self) -> str:
        return f"{self.name}[{self.expr}]"

    def start(self) -> None:
        if self._closing:
            msg = "Scheduled job is closing"
            raise RuntimeError(msg)
        if self._runner is None or self._runner.done():
            self._runner = create_task(self._run())

    async def close(self) -> None:
        if self._closing:
            msg = "Scheduled job is already closing"
            raise RuntimeError(msg)
        tasks = tuple(
            task for task in (self._runner, self._running) if task is not None
        )
        if current_task() in tasks:
            msg = "Scheduled jobs cannot close themselves"
            raise RuntimeError(msg)
        self._closing = True

        async def finish_close() -> None:
            for task in tasks:
                task.cancel()
            try:
                results = await gather(*tasks, return_exceptions=True)
            finally:
                self._runner = None
                self._running = None
                self._closing = False
            errors = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, CancelledError)
            ]
            _raise_errors("Scheduled job shutdown failed", errors)

        await await_cleanup(
            create_task(finish_close(), name=f"scheduled-job-close:{self.name}")
        )

    async def _run(self) -> None:
        while True:
            now = self.clock(self.timezone)
            next_at = cast(
                datetime,
                croniter(self.expr, now).get_next(datetime),
            )
            logger.debug("scheduled job next trigger: %s @ %s", self, next_at)
            await self.sleep(max(0, next_at.timestamp() - now.timestamp()))
            self._trigger()

    def _trigger(self) -> None:
        logger.debug("scheduled job trigger: %s", self)
        if self._running is not None and not self._running.done():
            logger.warning("scheduled job still running: %s", self)
            return

        try:
            target = self._resolve_target()
        except Exception:
            logger.exception("Scheduled job failed to resolve target: %s", self)
            return

        gateway, connection = target
        logger.debug(
            "scheduled job target: %s @ %s",
            self,
            connection or gateway or "-",
        )
        self._running = create_task(
            self._run_handler(gateway, connection),
            eager_start=False,
        )

    async def _run_handler(
        self,
        gateway: Gateway | None,
        connection: Connection | None,
    ) -> None:
        logger.debug("scheduled job run: %s", self)
        try:
            owner = cast(Task[None], current_task())
            with CURRENT_SCHEDULER_BOT.set((self.bot, owner)):
                await self._call_handler(gateway, connection)
        except Exception:
            logger.exception("Scheduled job failed: %s", self)
        else:
            logger.info("scheduled job done: %s", self)

    async def _call_handler(
        self,
        gateway: Gateway | None,
        connection: Connection | None,
    ) -> None:
        async with self.bot.container.enter_scope(Scope.REQUEST) as resolver:
            value = await call_with_injection(
                self.handler,
                InjectionContext(
                    bot=self.bot,
                    gateway=gateway,
                    connection=connection,
                ),
                resolver,
            )
        if value is not None:
            msg = "Scheduled task handlers must not return values"
            raise TypeError(msg)

    def _resolve_target(self) -> tuple[Gateway | None, Connection | None]:
        self_ = self.self_
        if self_ is None:
            return None, None

        gateway = self.bot.resolve_gateway(self.gateway_type)
        return gateway, gateway.connection_for(self_)


class CronScheduler:
    def __init__(
        self,
        bot: Bot,
        *,
        clock: Clock = datetime.now,
        sleep: Sleep = async_sleep,
        default_timezone: tzinfo | None = None,
    ) -> None:
        self.bot = bot
        self.clock = clock
        self.sleep = sleep
        self._default_timezone = (
            default_timezone if default_timezone is not None else UTC
        )
        self._jobs: list[CronJob] = []
        self._running = False
        self._closing = False

    @property
    def jobs(self) -> tuple[CronJob, ...]:
        return tuple(self._jobs)

    def on_cron(
        self,
        expr: str,
        *,
        name: str | None = None,
        timezone: str | None = None,
        self_: BotSelf | None = None,
        gateway: type[Gateway] | None = None,
    ) -> Callable:
        if gateway is not None and self_ is None:
            msg = "Scheduled gateway targets require self"
            raise ValueError(msg)

        def decorator(handler: Callable) -> Callable:
            if not croniter.is_valid(expr, strict=True):
                msg = f"Invalid cron expression: {expr}"
                raise ValueError(msg)

            job = CronJob(
                bot=self.bot,
                expr=expr,
                handler=handler,
                name=name or getattr(handler, "__name__", "cron_job"),
                timezone=(
                    ZoneInfo(timezone)
                    if timezone is not None
                    else self._default_timezone
                ),
                self_=self_,
                gateway_type=gateway,
                clock=self.clock,
                sleep=self.sleep,
            )
            self._jobs.append(job)
            if self._running:
                job.start()
            return handler

        return decorator

    def start(self) -> None:
        if self._closing or any(
            job._closing  # ruff: ignore[private-member-access] - scheduler owns jobs
            for job in self._jobs
        ):
            msg = "Scheduler is closing"
            raise RuntimeError(msg)
        self._running = True
        for job in self._jobs:
            job.start()

    async def close(self) -> None:
        if _scheduled_handler_is_active_for(self.bot):
            msg = "Scheduler cannot be closed from a scheduled handler"
            raise RuntimeError(msg)
        if self._closing:
            msg = "Scheduler is already closing"
            raise RuntimeError(msg)
        self._closing = True
        self._running = False
        try:
            results = await gather(
                *(job.close() for job in self._jobs),
                return_exceptions=True,
            )
        finally:
            self._closing = False
        errors = [result for result in results if isinstance(result, BaseException)]
        _raise_errors("Scheduler shutdown failed", errors)
