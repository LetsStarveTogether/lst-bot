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
    get_running_loop,
    timeout_at,
)
from asyncio import Event as AsyncEvent
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, suppress
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass
from datetime import timedelta, tzinfo
from types import TracebackType
from typing import TYPE_CHECKING, Self

from diwire import (
    Container,
    DependencyRegistrationPolicy,
    MissingPolicy,
    ResolverProtocol,
)
from logbook import Logger

from bot.gateways import Connection, Gateway
from bot.protocol.actions import ActionCall, ActionRequest, ActionResponse
from bot.protocol.common import BotSelf
from bot.protocol.enums import EventKind
from bot.protocol.events import Event
from bot.protocol.msg import Msg
from bot.protocol.returns import ReturnAction
from bot.routing import (
    DispatchEffect,
    DispatchResult,
    EventRoute,
    EventRouter,
    Permission,
    ReturnEffect,
    Rule,
)

from .di import (
    InjectionContext,
    State,
    call_with_injection,
    register_context_providers,
    request_scope,
)
from .scheduler import RECENT_SELF, CronScheduler, SelfTarget

if TYPE_CHECKING:
    from bot.gateways.base import RobynServer

logger = Logger(__name__)

_EVENT_QUEUE_CAPACITY = 64
_CURRENT_DISPATCHER: ContextVar[Bot | None] = ContextVar(
    "bot_current_dispatcher",
    default=None,
)


@dataclass(slots=True)
class _QueuedEvent:
    connection: Connection | None
    event: Event
    gateway: Gateway | None
    context: Context
    result: Future[list[DispatchResult]] | None = None
    deadline: float | None = None


class _DispatchTimeoutError(Exception):
    pass


class Bot:
    def __init__(
        self,
        *,
        admin_ids: Iterable[str] = (),
        cmd_prefixes: tuple[str, ...] = ("/",),
        dispatch_timeout: timedelta | None = timedelta(seconds=900),
        max_dispatches: int = 8,
        scheduler_timezone: tzinfo | None = None,
        container: Container | None = None,
    ) -> None:
        if isinstance(max_dispatches, bool) or not isinstance(max_dispatches, int):
            msg = "max_dispatches must be an integer"
            raise TypeError(msg)
        if max_dispatches <= 0:
            msg = "max_dispatches must be greater than zero"
            raise ValueError(msg)
        self.admin_ids = frozenset(admin_ids)
        self.cmd_prefixes = cmd_prefixes
        self.dispatch_timeout = dispatch_timeout
        self.max_dispatches = max_dispatches
        self.scheduler_timezone = scheduler_timezone
        self.container = (
            container
            if container is not None
            else Container(
                missing_policy=MissingPolicy.ERROR,
                dependency_registration_policy=DependencyRegistrationPolicy.IGNORE,
            )
        )
        self.container.add_instance(self, provides=Bot)
        self._gateways: list[Gateway] = []
        self._gateway_provider_types: set[type[Gateway]] = set()
        register_context_providers(self.container, self._gateway_provider_types)
        self._router = EventRouter()
        self._start_hooks: list[Callable] = []
        self._close_hooks: list[Callable] = []
        self._recent_connection: tuple[Gateway, BotSelf] | None = None
        self._scheduler = CronScheduler(self, default_timezone=self.scheduler_timezone)
        self._lifecycle_lock = Lock()
        self._started = False
        self._accepting_events = False
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
        register_context_providers(
            self.container,
            self._gateway_provider_types,
            gateway_type=type(gateway),
        )
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

    def add_router(self, router: EventRouter) -> EventRouter:
        self._router.add_router(router)
        return router

    @property
    def recent_connection(self) -> tuple[Gateway, BotSelf] | None:
        return self._recent_connection

    @property
    def scheduler(self) -> CronScheduler:
        return self._scheduler

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

    def on_event(
        self,
        event_type: EventKind | None = None,
        *,
        rule: Rule | Callable | None = None,
        permission: Permission | Callable | None = None,
        priority: int = 1,
        block: bool = False,
        name: str | None = None,
    ) -> Callable:
        return self._router.on_event(
            event_type,
            rule=rule,
            permission=permission,
            priority=priority,
            block=block,
            name=name,
        )

    def on_msg(
        self,
        *,
        rule: Rule | Callable | None = None,
        permission: Permission | Callable | None = None,
        priority: int = 1,
        block: bool = False,
        name: str | None = None,
    ) -> Callable:
        return self._router.on_msg(
            rule=rule,
            permission=permission,
            priority=priority,
            block=block,
            name=name,
        )

    def on_cmd(
        self,
        cmd: str,
        *,
        permission: Permission | Callable | None = None,
        priority: int = 1,
        block: bool = True,
        name: str | None = None,
    ) -> Callable:
        return self._router.on_cmd(
            cmd,
            permission=permission,
            priority=priority,
            block=block,
            name=name,
        )

    def on_cron(
        self,
        expr: str,
        *,
        name: str | None = None,
        timezone: str | None = None,
        self_: SelfTarget = RECENT_SELF,
        gateway: type[Gateway] | None = None,
    ) -> Callable:
        return self._scheduler.on_cron(
            expr,
            name=name,
            timezone=timezone,
            self_=self_,
            gateway=gateway,
        )

    def on_start(self, func: Callable) -> Callable:
        self._start_hooks.append(func)
        return func

    def on_close(self, func: Callable) -> Callable:
        self._close_hooks.append(func)
        return func

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            try:
                await self._start_once()
            except BaseException:
                logger.exception(
                    "bot startup failed: gateways={gateway_count}",
                    gateway_count=len(self._gateways),
                )
                raise
            self._started = True
            self._accepting_events = True
            self._running.set()

    async def close(self) -> None:
        if _CURRENT_DISPATCHER.get() is self:
            msg = "Bot cannot be closed from a dispatch handler"
            raise RuntimeError(msg)
        async with self._lifecycle_lock:
            if not self._started:
                return
            self._accepting_events = False
            self._running.clear()
            try:
                async with AsyncExitStack() as cleanup:
                    cleanup.push_async_callback(self.container.aclose)
                    for gateway in self._gateways:
                        cleanup.push_async_callback(gateway.close)
                    cleanup.push_async_callback(self._stop_dispatcher)
                    cleanup.push_async_callback(self._run_hooks, self._close_hooks)
                    cleanup.push_async_callback(self._scheduler.close)
            finally:
                self._started = False

    async def _start_once(self) -> None:
        async with AsyncExitStack() as rollback:
            await self._start_dispatcher()
            rollback.push_async_callback(self._stop_dispatcher)
            for gateway in self._gateways:
                rollback.push_async_callback(gateway.close)
                await gateway.start()
            rollback.push_async_callback(self._run_hooks, self._close_hooks)
            rollback.push_async_callback(self._scheduler.close)
            await self._run_hooks(self._start_hooks)
            self._scheduler.start()
            rollback.pop_all()

    async def wait_until_running(self) -> None:
        await self._running.wait()

    async def _run_hooks(self, hooks: list[Callable]) -> None:
        async with request_scope(self.container) as resolver:
            context = InjectionContext(bot=self)
            for hook in hooks:
                await call_with_injection(hook, context, resolver)

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
    ) -> list[DispatchResult]:
        if _CURRENT_DISPATCHER.get() is self:
            msg = "Recursive dispatch is not supported"
            raise RuntimeError(msg)
        result = get_running_loop().create_future()
        self._submit_event(connection, event, gateway=gateway, result=result)
        return await result

    def _submit_event(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None,
        result: Future[list[DispatchResult]] | None = None,
    ) -> None:
        queue = self._event_queue
        if not self._started or not self._accepting_events or queue is None:
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
        self._accepting_events = False
        workers, self._event_workers = self._event_workers, ()
        for worker in workers:
            worker.cancel()
        for worker in workers:
            with suppress(CancelledError):
                await worker

        queue, self._event_queue = self._event_queue, None
        if queue is None:
            return
        while True:
            try:
                item = queue.get_nowait()
            except QueueEmpty:
                return
            if item.result is not None and not item.result.done():
                item.result.cancel()
            queue.task_done()

    async def _dispatch_worker(self, queue: Queue[_QueuedEvent]) -> None:
        worker = current_task()
        while True:
            item = await queue.get()
            task = create_task(
                self._dispatch_queued_event(item),
                context=item.context,
            )
            try:
                results = await task
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
                        "queued event dispatch failed: {event} ({error})",
                        event=item.event,
                        error=type(exc).__name__,
                    )
            else:
                if item.result is not None and not item.result.done():
                    item.result.set_result(results)
            finally:
                queue.task_done()

    async def _dispatch_queued_event(
        self,
        item: _QueuedEvent,
    ) -> list[DispatchResult]:
        token = _CURRENT_DISPATCHER.set(self)
        try:
            return await self._dispatch_event(
                item.connection,
                item.event,
                gateway=item.gateway,
                deadline=item.deadline,
            )
        finally:
            _CURRENT_DISPATCHER.reset(token)

    async def _dispatch_event(
        self,
        connection: Connection | None,
        event: Event,
        *,
        gateway: Gateway | None,
        deadline: float | None,
    ) -> list[DispatchResult]:
        active_gateway = connection.gateway if connection is not None else gateway
        self._remember_recent_connection(active_gateway, event)

        logger.info(
            "dispatch event: {event} via {gateway}",
            event=event,
            gateway=active_gateway or "-",
        )
        if __debug__:
            logger.trace(
                "dispatch event : {event!r} {gateway}",
                event=event,
                gateway=active_gateway,
            )

        async with request_scope(self.container) as resolver:
            state: State = {}
            results: list[DispatchResult] = []

            for route in self._router.routes:
                if route.event_type is not None and route.event_type != event.type:
                    continue
                context = InjectionContext(
                    bot=self,
                    gateway=active_gateway,
                    connection=connection,
                    event=event,
                    state=state,
                    route=route,
                )
                route_result = await self._dispatch_route(
                    context,
                    route,
                    resolver,
                    deadline,
                )
                if route_result is None:
                    continue

                result, timed_out = route_result
                results.append(result)
                if timed_out:
                    break
                if result.exception is None and route.block:
                    break

            if __debug__:
                logger.trace(
                    "dispatch event done : {event!r} {gateway} {results!r}",
                    event=event,
                    gateway=active_gateway,
                    results=results,
                )
            return results

    def _remember_recent_connection(
        self,
        gateway: Gateway | None,
        event: Event,
    ) -> None:
        self_ = event.self_
        if gateway is not None and self_ is not None:
            if __debug__ and self._recent_connection != (gateway, self_):
                logger.trace(
                    "remember recent connection : {gateway} {self_}",
                    gateway=gateway,
                    self_=self_,
                )
            self._recent_connection = (gateway, self_)

    async def _dispatch_route(
        self,
        context: InjectionContext,
        route: EventRoute,
        resolver: ResolverProtocol,
        deadline: float | None,
    ) -> tuple[DispatchResult, bool] | None:
        try:
            matched = await self._before_deadline(
                deadline,
                lambda: route.check(context, resolver),
            )
        except _DispatchTimeoutError:
            exc = TimeoutError()
            self._log_dispatch_timeout(context, route)
            return self._failed_dispatch_result(context, route, exc), True
        except Exception as exc:
            self._log_dispatch_exception(context, route, exc)
            return self._failed_dispatch_result(context, route, exc), False

        if not matched:
            return None

        if __debug__:
            logger.trace(
                "route matched : {route!r} {event!r}",
                route=route,
                event=context.event,
            )

        try:
            result = await self._before_deadline(
                deadline,
                lambda: self._run_route(context, route, resolver),
            )
        except _DispatchTimeoutError:
            exc = TimeoutError()
            self._log_dispatch_timeout(context, route)
            return self._failed_dispatch_result(context, route, exc), True
        except Exception as exc:
            self._log_dispatch_exception(context, route, exc)
            return self._failed_dispatch_result(context, route, exc), False

        return result, False

    async def _before_deadline[T](
        self,
        deadline: float | None,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        if deadline is None:
            return await operation()
        if get_running_loop().time() >= deadline:
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
    ) -> DispatchResult:
        values: list[object] = []
        effects: list[DispatchEffect] = []
        exception: BaseException | None = None
        try:
            for handler in route.handlers:
                self._log_handler_run(context, route, handler)
                value = await call_with_injection(handler, context, resolver)
                if value is not None:
                    values.append(value)
                    await self._execute_return_value(context, value, effects)
        except Exception as exc:
            exception = exc
            self._log_dispatch_exception(context, route, exc)

        state = context.state
        if state is None:
            msg = "Dispatch context must carry event state"
            raise TypeError(msg)
        return DispatchResult(
            route=route,
            values=values,
            state=state,
            effects=effects,
            exception=exception,
        )

    def _log_handler_run(
        self,
        context: InjectionContext,
        route: EventRoute,
        handler: Callable,
    ) -> None:
        if not __debug__:
            return
        logger.trace(
            "run handler : {handler} {route!r} {event!r}",
            handler=handler,
            route=route,
            event=context.event,
        )

    def _failed_dispatch_result(
        self,
        context: InjectionContext,
        route: EventRoute,
        exception: BaseException,
    ) -> DispatchResult:
        state = context.state
        if state is None:
            msg = "Dispatch context must carry event state"
            raise TypeError(msg)
        return DispatchResult(
            route=route,
            values=[],
            state=state,
            effects=[],
            exception=exception,
        )

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
            "Dispatch route failed: {route} @ {event} via {gateway} ({error})",
            route=route,
            event=event,
            gateway=gateway or "-",
            error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
        )

    def _log_dispatch_timeout(
        self,
        context: InjectionContext,
        route: EventRoute,
    ) -> None:
        event = context.event
        gateway = context.gateway
        logger.warning(
            "Dispatch route timed out: {route} @ {event} via {gateway}",
            route=route,
            event=event,
            gateway=gateway or "-",
        )

    async def _execute_return_value(
        self,
        context: InjectionContext,
        value: object,
        effects: list[DispatchEffect],
    ) -> None:
        if isinstance(value, list | tuple):
            for item in value:
                await self._execute_return_value(context, item, effects)
            return

        action = self._return_action_from_value(value)
        await self._execute_return_action(context, action, effects)

    def _return_action_from_value(self, value: object) -> ReturnAction:
        if isinstance(value, ReturnAction):
            return value
        if isinstance(value, str | Msg):
            return ReturnAction.message(value)
        if isinstance(value, ActionCall):
            return ReturnAction.from_call(value)

        msg = f"Unsupported handler return value: {type(value).__name__}"
        raise TypeError(msg)

    async def _execute_return_action(
        self,
        context: InjectionContext,
        action: ReturnAction,
        effects: list[DispatchEffect],
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

        route = context.route
        gateway = context.gateway or connection.gateway
        if __debug__:
            logger.debug(
                "execute return action: {action} @ {event} via {connection}",
                action=action,
                event=event or "-",
                connection=connection,
            )
            logger.trace(
                "execute return action : {action!r} {route!r} {event!r} "
                "{gateway} {connection}",
                action=action,
                route=route,
                event=event,
                gateway=gateway,
                connection=connection,
            )
        try:
            outcome = await connection.execute_return_action(event, action)
        except Exception as exc:
            error = str(exc)
            logger.exception(
                "return action failed: {action} @ {event} via {connection} ({error})",
                action=action,
                event=event or "-",
                connection=connection,
                error=f"{type(exc).__name__}: {error}" if error else type(exc).__name__,
            )
            raise

        if __debug__:
            outcome_text = (
                str(outcome)
                if isinstance(outcome, ActionRequest | ActionResponse)
                else type(outcome).__name__
            )
            logger.debug(
                "return action done: {action} @ {event} = {outcome}",
                action=action,
                event=event or "-",
                outcome=outcome_text,
            )
            logger.trace(
                "return action done : {action!r} {route!r} {event!r} "
                "{gateway} {connection} {outcome!r}",
                action=action,
                route=route,
                event=event,
                gateway=gateway,
                connection=connection,
                outcome=outcome,
            )
        effects.append(ReturnEffect(action=action, outcome=outcome))
