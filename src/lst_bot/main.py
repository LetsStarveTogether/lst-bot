from __future__ import annotations

from asyncio import Event as AsyncEvent
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from types import TracebackType
from typing import Any, Self

import uvloop
from bot import Bot, BotSelf, Event, Injected
from bot.gateways.onebot11 import ForwardWebSocket, OneBot11Gateway, WebSocketAction
from hitokoto import HitokotoClient
from klei import KleiClient
from logbook import Logger
from lst import LstClient
from urllib3_future import AsyncProxyManager

from .agent import DstQuestionAgent
from .general import register_crons
from .general import router as general_router
from .question import router as question_router
from .rooms import router as rooms_router
from .settings import Settings, configure_logging

logger = Logger(__name__)


class Application:
    def __init__(
        self,
        bot: Bot,
        resources: tuple[AbstractAsyncContextManager[Any], ...] = (),
    ) -> None:
        self.bot = bot
        self.resources = resources
        self._exit_stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        if self._exit_stack is not None:
            msg = "Application is already running"
            raise RuntimeError(msg)

        async with AsyncExitStack() as stack:
            for resource in self.resources:
                await stack.enter_async_context(resource)
            await stack.enter_async_context(self.bot)
            self._exit_stack = stack.pop_all()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        stack, self._exit_stack = self._exit_stack, None
        if stack is None:
            return None
        return await stack.__aexit__(exc_type, exc, traceback)

    async def run(self) -> None:
        async with self:
            await AsyncEvent().wait()


def build_application(settings: Settings) -> Application:
    bot = Bot(
        admin_ids=settings.bot_admin,
        cmd_prefixes=settings.bot_cmd_prefixes,
        dispatch_timeout=settings.bot_timeout,
        scheduler_timezone=settings.bot_timezone,
    )
    gateway = OneBot11Gateway(
        bot,
        ingress=[
            ForwardWebSocket(
                settings.onebot_ws_url,
                role="universal",
                self_=BotSelf(platform="qq", user_id=settings.onebot_self_id),
            )
        ],
        action=WebSocketAction(),
        access_token=settings.onebot_access_token,
    )
    bot.add_gateway(gateway)

    lst_client = LstClient()
    hitokoto_client = HitokotoClient(
        http_pool=AsyncProxyManager(settings.http_proxy),
    )
    klei_client = KleiClient(
        access_token=settings.klei_access_token,
        http_pool=AsyncProxyManager(settings.http_proxy),
    )
    question_agent = DstQuestionAgent(
        openrouter_api_key=settings.openrouter_api_key,
        dosu_mcp_endpoint=settings.dosu_mcp_endpoint,
        dosu_api_key=settings.dosu_api_key,
        http_proxy=settings.http_proxy,
    )

    for instance in (
        settings,
        lst_client,
        hitokoto_client,
        klei_client,
        question_agent,
    ):
        bot.container.add_instance(instance)

    for router in (general_router, question_router, rooms_router):
        bot.add_router(router)
    register_crons(bot)

    @bot.on_event()
    def log_event(event: Injected[Event]) -> None:
        if __debug__:
            logger.trace("receive event : {event}", event=event)

    return Application(
        bot,
        resources=(hitokoto_client, klei_client, question_agent),
    )


def main() -> None:
    settings = Settings()
    with configure_logging(settings):
        uvloop.run(build_application(settings).run())


if __name__ == "__main__":
    main()


__all__ = ["Application", "build_application", "main"]
