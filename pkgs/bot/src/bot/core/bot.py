from __future__ import annotations

from asyncio import (
    CancelledError,
    Future,
    Lock,
    Queue,
    QueueEmpty,
    Task,
    create_task,
    current_task,
    gather,
    get_running_loop,
    timeout_at,
)
from asyncio import Event as AsyncEvent
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass
from datetime import timedelta, tzinfo
from functools import partial
from logging import getLogger
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Self, cast

from diwire import (
    Container,
    DependencyRegistrationPolicy,
    MissingPolicy,
    ResolverProtocol,
    Scope,
)

from bot.gateways import Connection, Gateway
from bot.protocol.actions import ActionCall
from bot.protocol.events import Event
from bot.protocol.msg import Msg
from bot.protocol.returns import ReturnAction
from bot.routing import EventRoute, EventRouter

from .di import (
    InjectionContext,
    call_with_injection,
    register_context_providers,
)
from .scheduler import (
    CURRENT_SCHEDULER_BOT,
    CronScheduler,
    _raise_errors,
)

if TYPE_CHECKING:
    from bot.gateways.base import RobynServer

logger = getLogger(__name__)

_EVENT_QUEUE_CAPACITY = 64
type _TaskOwner = tuple[object, Task[None]]

_CURRENT_DISPATCHER: ContextVar[_TaskOwner | None] = ContextVar(
    "bot_current_dispatcher",
    default=None,
)
_CURRENT_LIFECYCLE: ContextVar[Bot | None] = ContextVar(
    "bot_current_lifecycle",
    default=None,
)
type _CleanupCallback = Callable[[], Awaitable[None]]


def _task_is_active_for(owner: _TaskOwner | None, bot: Bot) -> bool:
    return owner is not None and owner[0] is bot and not owner[1].done()


@dataclass(slots=True)
class _QueuedEvent:
    connection: Connection | None
    event: Event
    gateway: Gateway | None
    context: Context
    result: Future[None] | None = None
    deadline: float | None = None


class _DispatchTimeoutError(Exception):
    pass


class Bot(EventRouter):
    def __init__(
        self,
        *,
        admin_ids: Mapping[str, Iterable[str]] | None = None,
        cmd_prefixes: tuple[str, ...] = ("/",),
        dispatch_timeout: timedelta | None = timedelta(seconds=900),
        max_dispatches: int = 8,
        scheduler_timezone: tzinfo | None = None,
    ) -> None:
        super().__init__()
        if isinstance(max_dispatches, bool) or not isinstance(max_dispatches, int):
            msg = "max_dispatches must be an integer"
            raise TypeError(msg)
        if max_dispatches <= 0:
            msg = "max_dispatches must be greater than zero"
            raise ValueError(msg)
        admins: dict[str, frozenset[str]] = {}
        for platform, user_ids in (admin_ids or {}).items():
            if not isinstance(platform, str) or isinstance(user_ids, str):
                msg = "admin_ids must map platform names to user ID iterables"
                raise TypeError(msg)
            users = frozenset(user_ids)
            if not all(isinstance(user_id, str) for user_id in users):
                msg = "admin IDs must be strings"
                raise TypeError(msg)
            admins[platform] = users
        self.admin_ids: Mapping[str, frozenset[str]] = MappingProxyType(admins)
        self.cmd_prefixes = cmd_prefixes
        self.dispatch_timeout = dispatch_timeout
        self.max_dispatches = max_dispatches
        self.container = Container(
            missing_policy=MissingPolicy.ERROR,
            dependency_registration_policy=DependencyRegistrationPolicy.IGNORE,
            use_resolver_context=False,
        )
        self.container.add_instance(self, provides=Bot)
        self._gateways: list[Gateway] = []
        self.scheduler = CronScheduler(self, default_timezone=scheduler_timezone)
        self._lifecycle_lock = Lock()
        self._lifecycle_started = False
        self._pending_cleanup: list[_CleanupCallback] = []
        self._running = AsyncEvent()
        self._event_queue: Queue[_QueuedEvent] | None = None
        self._event_workers: tuple[Task[None], ...] = ()
        self._mounted_server: RobynServer | None = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback
        await self.close()

    def add_gateway(self, gateway: Gateway) -> None:
        if gateway.bot is not self:
            msg = "Gateway belongs to another bot"
            raise ValueError(msg)
        if self._lifecycle_started:
            msg = "Gateways cannot be added after bot startup begins"
            raise RuntimeError(msg)
        self._gateways.append(gateway)

    def mount_server(self, server: RobynServer) -> None:
        if self._mounted_server is server:
            return
        if self._mounted_server is not None:
            msg = "A bot can only be mounted on one server"
            raise ValueError(msg)
        server.startup_handler(self.start)
        server.shutdown_handler(self.close)
        self._mounted_server = server

    def resolve_gateway(self, gateway_type: type[Gateway] | None = None) -> Gateway:
        if gateway_type is None:
            if len(self._gateways) == 1:
                return self._gateways[0]
            if not self._gateways:
                msg = "No gateways are registered"
                raise LookupError(msg)
            msg = "Gateway type is required when multiple gateways are registered"
            raise LookupError(msg)

        gateways = [
            gateway for gateway in self._gateways if isinstance(gateway, gateway_type)
        ]
        if len(gateways) == 1:
            return gateways[0]
        if not gateways:
            msg = f"No gateway of type {gateway_type.__name__} is registered"
            raise LookupError(msg)
        msg = f"Multiple gateways of type {gateway_type.__name__} are registered"
        raise LookupError(msg)

    async def start(self) -> None:
        self._reject_lifecycle_reentry()
        if not self._lifecycle_started:
            register_context_providers(self.container)
            self._lifecycle_started = True
        async with self._lifecycle_lock:
            with _CURRENT_LIFECYCLE.set(self):
                if self._pending_cleanup:
                    msg = "Bot shutdown is incomplete; call close() again"
                    raise RuntimeError(msg)
                if self._running.is_set():
                    return
                try:
                    await self._start_once()
                except BaseException:
                    logger.exception(
                        "bot startup failed: gateways=%s",
                        len(self._gateways),
                    )
                    raise
                self._running.set()

    async def close(self) -> None:
        if _task_is_active_for(_CURRENT_DISPATCHER.get(), self):
            msg = "Bot cannot be closed from a dispatch handler"
            raise RuntimeError(msg)
        if _task_is_active_for(CURRENT_SCHEDULER_BOT.get(), self):
            msg = "Bot cannot be closed from a scheduled handler"
            raise RuntimeError(msg)
        self._reject_lifecycle_reentry()
        async with self._lifecycle_lock:
            with _CURRENT_LIFECYCLE.set(self):
                if not self._running.is_set() and not self._pending_cleanup:
                    return
                self._running.clear()
                if not self._pending_cleanup:
                    self._pending_cleanup = self._cleanup_callbacks(
                        self._gateways,
                        close_container=True,
                    )
                failed, errors = await self._run_cleanup(self._pending_cleanup)
                self._pending_cleanup = failed
                _raise_errors("Bot cleanup failed", errors)

    async def _start_once(self) -> None:
        gateways: list[Gateway] = []
        try:
            await self._start_dispatcher()
            for gateway in self._gateways:
                gateways.append(gateway)
                await gateway.start()
            self.container.compile()
            self.scheduler.start()
        except BaseException as startup_error:
            callbacks = self._cleanup_callbacks(
                gateways,
                close_container=False,
            )
            failed, cleanup_errors = await self._run_cleanup(callbacks)
            self._pending_cleanup = failed
            if cleanup_errors:
                msg = "Bot startup and rollback failed"
                errors = [startup_error, *cleanup_errors]
                raise BaseExceptionGroup(msg, errors) from None
            raise

    def _reject_lifecycle_reentry(self) -> None:
        if _CURRENT_LIFECYCLE.get() is self and self._lifecycle_lock.locked():
            msg = "Bot lifecycle cannot be re-entered"
            raise RuntimeError(msg)

    def _cleanup_callbacks(
        self,
        gateways: Iterable[Gateway],
        *,
        close_container: bool,
    ) -> list[_CleanupCallback]:
        callbacks: list[_CleanupCallback] = [
            self.scheduler.close,
            self._stop_dispatcher,
        ]
        callbacks.extend(gateway.close for gateway in reversed(tuple(gateways)))
        if close_container:
            callbacks.append(self._close_container)
        return callbacks

    async def _close_container(self) -> None:
        await self.container.aclose()
        # Re-registration discards DIWire's closed resolver, not its providers.
        self.container.add_instance(self, provides=Bot)

    @staticmethod
    async def _run_cleanup(
        callbacks: Iterable[_CleanupCallback],
    ) -> tuple[list[_CleanupCallback], list[BaseException]]:
        failed: list[_CleanupCallback] = []
        errors: list[BaseException] = []
        for callback in callbacks:
            try:
                await callback()
            except BaseException as exc:
                failed.append(callback)
                errors.append(exc)
        return failed, errors

    async def wait_until_running(self) -> None:
        await self._running.wait()

    def enqueue_event(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None = None,
    ) -> None:
        self._submit_event(connection, event, gateway=gateway)

    async def dispatch(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None = None,
    ) -> None:
        if _task_is_active_for(_CURRENT_DISPATCHER.get(), self):
            msg = "Recursive dispatch is not supported"
            raise RuntimeError(msg)
        result = get_running_loop().create_future()
        self._submit_event(connection, event, gateway=gateway, result=result)
        await result

    def _submit_event(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None,
        result: Future[None] | None = None,
    ) -> None:
        queue = self._event_queue
        if not self._running.is_set() or queue is None:
            msg = "Bot is not running"
            raise RuntimeError(msg)
        timeout = self.dispatch_timeout
        deadline = (
            None
            if timeout is None
            else get_running_loop().time() + timeout.total_seconds()
        )
        queue.put_nowait(
            _QueuedEvent(
                connection=connection,
                event=event,
                gateway=gateway,
                context=copy_context(),
                result=result,
                deadline=deadline,
            )
        )

    async def _start_dispatcher(self) -> None:
        queue: Queue[_QueuedEvent] = Queue(maxsize=_EVENT_QUEUE_CAPACITY)
        self._event_queue = queue
        self._event_workers = tuple(
            create_task(
                self._dispatch_worker(queue),
                name=f"bot-dispatch-{index + 1}",
            )
            for index in range(self.max_dispatches)
        )

    async def _stop_dispatcher(self) -> None:
        workers = self._event_workers
        for worker in workers:
            worker.cancel()
        results = await gather(*workers, return_exceptions=True)
        self._event_workers = ()

        queue, self._event_queue = self._event_queue, None
        if queue is not None:
            while True:
                try:
                    item = queue.get_nowait()
                except QueueEmpty:
                    break
                if item.result is not None and not item.result.done():
                    item.result.cancel()

        errors = [
            result
            for result in results
            if isinstance(result, BaseException)
            and not isinstance(result, CancelledError)
        ]
        _raise_errors("Bot dispatcher shutdown failed", errors)

    async def _dispatch_worker(self, queue: Queue[_QueuedEvent]) -> None:
        worker = current_task()
        while True:
            item = await queue.get()
            task = create_task(
                self._dispatch_queued_event(item),
                context=item.context,
            )
            try:
                await task
            except CancelledError:
                if item.result is not None and not item.result.done():
                    item.result.cancel()
                if worker is not None and worker.cancelling():
                    raise
            except BaseException as exc:
                if item.result is not None and not item.result.done():
                    item.result.set_exception(exc)
                else:
                    logger.exception(
                        "queued event dispatch failed: %s",
                        item.event,
                    )
            else:
                if item.result is not None and not item.result.done():
                    item.result.set_result(None)

    async def _dispatch_queued_event(
        self,
        item: _QueuedEvent,
    ) -> None:
        owner = cast(Task[None], current_task())
        with _CURRENT_DISPATCHER.set((self, owner)):
            await self._dispatch_event(
                item.connection,
                item.event,
                gateway=item.gateway,
                deadline=item.deadline,
            )

    async def _dispatch_event(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None,
        deadline: float | None,
    ) -> None:
        active_gateway = connection.gateway if connection is not None else gateway

        logger.info(
            "dispatch event: %s/%s#%s via %s",
            event.type,
            event.detail_type,
            event.id,
            active_gateway or "-",
        )

        async with self.container.enter_scope(Scope.REQUEST) as resolver:
            for route in tuple(self.routes):
                if route.event_type is not None and route.event_type != event.type:
                    continue
                context = InjectionContext(
                    bot=self,
                    gateway=active_gateway,
                    connection=connection,
                    event=event,
                )
                try:
                    if not await self._before_deadline(
                        deadline,
                        partial(route.rule, context, resolver),
                    ):
                        continue
                    await self._before_deadline(
                        deadline,
                        partial(self._run_route, context, route, resolver),
                    )
                except _DispatchTimeoutError:
                    self._log_dispatch_timeout(context, route)
                    break
                except Exception as exc:
                    self._log_dispatch_exception(context, route, exc)
                else:
                    if route.block:
                        break

    async def _before_deadline[T](
        self,
        deadline: float | None,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        if deadline is not None and get_running_loop().time() >= deadline:
            raise _DispatchTimeoutError

        timeout_scope = timeout_at(deadline)
        try:
            async with timeout_scope:
                return await operation()
        except TimeoutError:
            if timeout_scope.expired():
                raise _DispatchTimeoutError from None
            raise

    async def _run_route(
        self,
        context: InjectionContext,
        route: EventRoute,
        resolver: ResolverProtocol,
    ) -> None:
        value = await call_with_injection(route.handler, context, resolver)
        if value is not None:
            await self._execute_return_value(context, value)

    def _log_dispatch_exception(
        self,
        context: InjectionContext,
        route: EventRoute,
        exc: BaseException,
    ) -> None:
        event = context.event
        gateway = context.gateway
        error = str(exc)
        logger.error(
            "Dispatch route failed: %s @ %s via %s (%s)",
            route,
            event,
            gateway or "-",
            f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            exc_info=exc,
        )

    def _log_dispatch_timeout(
        self,
        context: InjectionContext,
        route: EventRoute,
    ) -> None:
        event = context.event
        gateway = context.gateway
        logger.warning(
            "Dispatch route timed out: %s @ %s via %s",
            route,
            event,
            gateway or "-",
        )

    async def _execute_return_value(
        self,
        context: InjectionContext,
        value: object,
    ) -> None:
        if isinstance(value, list | tuple):
            for item in value:
                await self._execute_return_value(context, item)
            return

        action = self._return_action_from_value(value)
        await self._execute_return_action(context, action)

    def _return_action_from_value(self, value: object) -> ReturnAction:
        if isinstance(value, ReturnAction):
            return value
        if isinstance(value, str | Msg):
            return ReturnAction.message(value)
        if isinstance(value, ActionCall):
            return ReturnAction(kind="call", action_call=value)

        msg = f"Unsupported handler return value: {type(value).__name__}"
        raise TypeError(msg)

    async def _execute_return_action(
        self,
        context: InjectionContext,
        action: ReturnAction,
    ) -> None:
        event = context.event
        connection = context.connection
        if connection is None:
            self_ = (
                action.self_
                if action.kind == "call" and action.self_ is not None
                else event.self_
                if event is not None
                else None
            )
            if context.gateway is None or self_ is None:
                msg = "Return actions require a connection or self"
                raise TypeError(msg)
            connection = context.gateway.connection_for(self_)

        await (context.gateway or connection.gateway).execute_return_action(
            connection,
            event,
            action,
        )
