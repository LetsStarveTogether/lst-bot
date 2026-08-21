from __future__ import annotations

from asyncio import CancelledError, Task, create_task, current_task, gather
from asyncio import sleep as async_sleep
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, tzinfo
from logging import getLogger
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from croniter import croniter

from bot.gateways import Connection, Gateway
from bot.protocol.common import BotSelf

from .di import InjectionContext, call_with_injection, inject, request_scope

if TYPE_CHECKING:
    from .bot import Bot


logger = getLogger(__name__)

type Sleep = Callable[[float], Awaitable[object]]
type Clock = Callable[[tzinfo], datetime]

CURRENT_SCHEDULER_BOT: ContextVar[tuple[object, Task[None]] | None] = ContextVar(
    "bot_current_scheduler",
    default=None,
)


def _raise_errors(message: str, errors: list[BaseException]) -> None:
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup(message, errors)


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

    def __post_init__(self) -> None:
        self.handler = inject(self.handler)

    def __str__(self) -> str:
        return f"{self.name}[{self.expr}]"

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._runner = create_task(self._run())

    async def close(self) -> None:
        tasks = tuple(
            task for task in (self._runner, self._running) if task is not None
        )
        if current_task() in tasks:
            msg = "Scheduled jobs cannot close themselves"
            raise RuntimeError(msg)
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await gather(*tasks, return_exceptions=True)
        self._runner = None
        self._running = None
        errors = [
            result
            for result in results
            if isinstance(result, BaseException)
            and not isinstance(result, CancelledError)
        ]
        _raise_errors("Scheduled job shutdown failed", errors)

    async def _run(self) -> None:
        while True:
            now = self.clock(self.timezone)
            next_at = cast(
                datetime,
                croniter(self.expr, now).get_next(datetime),
            )
            logger.debug("scheduled job next trigger: %s @ %s", self, next_at)
            await self.sleep(max(0, next_at.timestamp() - now.timestamp()))
            await self._trigger()

    async def _trigger(self) -> None:
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
        self._running = create_task(self._run_handler(gateway, connection))

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
        async with request_scope(self.bot.container) as resolver:
            context = InjectionContext(
                bot=self.bot,
                gateway=gateway,
                connection=connection,
            )
            value = await call_with_injection(self.handler, context, resolver)
        if value is not None:
            msg = "Scheduled task handlers must not return values"
            raise TypeError(msg)

    def _resolve_target(self) -> _Target:
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
        self._default_timezone = default_timezone
        self._jobs: list[CronJob] = []
        self._running = False

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
        def decorator(handler: Callable) -> Callable:
            if not croniter.is_valid(expr, strict=True):
                msg = f"Invalid cron expression: {expr}"
                raise ValueError(msg)

            job = CronJob(
                bot=self.bot,
                expr=expr,
                handler=handler,
                name=name or getattr(handler, "__name__", "cron_job"),
                timezone=self._timezone(timezone),
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
        self._running = True
        for job in self._jobs:
            job.start()

    async def close(self) -> None:
        self._running = False
        errors: list[BaseException] = []
        for job in self._jobs:
            try:
                await job.close()
            except BaseException as exc:
                errors.append(exc)
        _raise_errors("Scheduler shutdown failed", errors)

    def _timezone(self, timezone: str | None) -> tzinfo:
        if timezone is not None:
            return ZoneInfo(timezone)
        if self._default_timezone is not None:
            return self._default_timezone
        return cast(tzinfo, datetime.now().astimezone().tzinfo)


_Target = tuple[Gateway | None, Connection | None]
