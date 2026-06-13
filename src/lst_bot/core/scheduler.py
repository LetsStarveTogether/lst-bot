from __future__ import annotations

from asyncio import CancelledError, Task, create_task
from asyncio import sleep as async_sleep
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from croniter import croniter
from diwire import Scope
from logbook import Logger

from lst_bot.gateways import Connection, Gateway
from lst_bot.protocol.common import BotSelf

from .di import InjectionContext, State, call_with_injection

if TYPE_CHECKING:
    from .bot import Bot


logger = Logger(__name__)

type Sleep = Callable[[float], Awaitable[object]]
type Clock = Callable[[tzinfo], datetime]


@dataclass(frozen=True, slots=True)
class RecentSelf:
    def __str__(self) -> str:
        return "recent"


type SelfTarget = BotSelf | RecentSelf | None

RECENT_SELF = RecentSelf()


@dataclass(slots=True)
class CronJob:
    bot: Bot
    expr: str
    handler: Callable
    name: str
    timezone: tzinfo
    self_: SelfTarget
    gateway_type: type[Gateway] | None
    clock: Clock
    sleep: Sleep
    _runner: Task[None] | None = None
    _running: Task[None] | None = None

    def __str__(self) -> str:
        return f"{self.name}[{self.expr}]"

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._runner = create_task(self._run())

    async def close(self) -> None:
        tasks = (self._runner, self._running)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(CancelledError):
                    await task
        self._runner = None
        self._running = None

    async def _run(self) -> None:
        while True:
            now = self.clock(self.timezone)
            next_at = cast(
                datetime,
                croniter(self.expr, now).get_next(datetime),
            )
            if __debug__:
                logger.debug(
                    "scheduled job next trigger: {job} @ {next_at}",
                    job=self,
                    next_at=next_at,
                )
            await self.sleep(max(0, (next_at - now).total_seconds()))
            await self._trigger()

    async def _trigger(self) -> None:
        if __debug__:
            logger.debug("scheduled job trigger: {job}", job=self)
        if self._running is not None and not self._running.done():
            logger.warning(
                "scheduled job still running: {job}",
                job=self,
            )
            return

        try:
            target = self._resolve_target()
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "Scheduled job failed to resolve target: {job} ({error})",
                job=self,
                error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            )
            return

        if isinstance(target, _Skip):
            return

        gateway, connection = target
        if __debug__:
            logger.debug(
                "scheduled job target: {job} @ {target}",
                job=self,
                target=connection or gateway or "-",
            )
        self._running = create_task(self._run_handler(gateway, connection))

    async def _run_handler(
        self,
        gateway: Gateway | None,
        connection: Connection | None,
    ) -> None:
        try:
            if __debug__:
                logger.debug("scheduled job run: {job}", job=self)
            await self._call_handler(gateway, connection)
        except CancelledError:
            raise
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "Scheduled job failed: {job} ({error})",
                job=self,
                error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            )
        else:
            logger.info("scheduled job done: {job}", job=self)

    async def _call_handler(
        self,
        gateway: Gateway | None,
        connection: Connection | None,
    ) -> None:
        async with self.bot.container.enter_scope(Scope.REQUEST) as resolver:
            state: State = {}
            context = InjectionContext(
                bot=self.bot,
                gateway=gateway,
                connection=connection,
                state=state,
            )
            value = await call_with_injection(self.handler, context, resolver)
        if value is not None:
            msg = "Scheduled task handlers must not return values"
            raise TypeError(msg)

    def _resolve_target(self) -> _Target | _Skip:
        self_ = self.self_
        if self_ is None:
            return None, None

        if isinstance(self_, RecentSelf):
            recent = self.bot.recent_connection
            if recent is None:
                logger.warning(
                    "scheduled job skipped: {job} ({reason})",
                    job=self,
                    reason="no recent bot account exists",
                )
                return _SKIP
            gateway, self_ = recent
            if self.gateway_type is not None and not isinstance(
                gateway,
                self.gateway_type,
            ):
                logger.warning(
                    "scheduled job skipped: {job} {actual}!={expected}",
                    job=self,
                    actual=type(gateway).__name__,
                    expected=self.gateway_type.__name__,
                )
                return _SKIP
            return gateway, gateway.connection_for(self_)

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
        self._clock = clock
        self._sleep = sleep
        self._default_timezone = (
            default_timezone if default_timezone is not None else bot.scheduler_timezone
        )
        self._jobs: list[CronJob] = []
        self._running = False

    @property
    def jobs(self) -> tuple[CronJob, ...]:
        return tuple(self._jobs)

    def add_cron(
        self,
        expr: str,
        handler: Callable,
        *,
        name: str | None = None,
        timezone: str | None = None,
        self_: SelfTarget = RECENT_SELF,
        gateway: type[Gateway] | None = None,
    ) -> CronJob:
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
            clock=self._clock,
            sleep=self._sleep,
        )
        self._jobs.append(job)
        if self._running:
            job.start()
        return job

    def on_cron(
        self,
        expr: str,
        *,
        name: str | None = None,
        timezone: str | None = None,
        self_: SelfTarget = RECENT_SELF,
        gateway: type[Gateway] | None = None,
    ) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.add_cron(
                expr,
                handler,
                name=name,
                timezone=timezone,
                self_=self_,
                gateway=gateway,
            )
            return handler

        return decorator

    def start(self) -> None:
        self._running = True
        for job in self._jobs:
            job.start()

    async def close(self) -> None:
        self._running = False
        for job in self._jobs:
            await job.close()

    def _timezone(self, timezone: str | None) -> tzinfo:
        if timezone is not None:
            return ZoneInfo(timezone)
        if self._default_timezone is not None:
            return self._default_timezone
        local = datetime.now().astimezone().tzinfo
        if local is None:
            msg = "Could not determine local scheduler timezone"
            raise RuntimeError(msg)
        return local


_Target = tuple[Gateway | None, Connection | None]


class _Skip:
    pass


_SKIP = _Skip()


__all__ = [
    "RECENT_SELF",
    "CronJob",
    "CronScheduler",
    "RecentSelf",
    "SelfTarget",
]
