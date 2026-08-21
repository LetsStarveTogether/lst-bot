import asyncio
import logging
from functools import partial

from bot import Bot, BotSelf
from bot.gateways.base import connect_websocket
from bot.gateways.discord import DiscordGateway, DiscordIntent
from bot.gateways.onebot11 import ForwardWebSocket, OneBot11Gateway, WebSocketAction
from bot.gateways.telegram import TelegramGateway
from hitokoto import HitokotoClient
from httpx import AsyncClient
from klei import KleiClient
from logbook.compat import redirected_logging
from logbook.more import ColorizedStderrHandler
from lst import LstClient
from pydantic_ai import Agent
from urllib3_future import AsyncProxyManager

from .agent import REQUEST_TIMEOUT, build_question_agent
from .general import report
from .general import router as general_router
from .question import router as question_router
from .rooms import router as rooms_router
from .settings import Settings


def build_bot(
    settings: Settings,
    *,
    http_pool: AsyncProxyManager,
    question_agent: Agent,
) -> Bot:
    bot = Bot(
        admin_ids=settings.bot_admin,
        cmd_prefixes=settings.bot_cmd_prefixes,
        dispatch_timeout=settings.bot_timeout,
        scheduler_timezone=settings.bot_timezone,
    )
    if settings.onebot_ws_url:
        onebot_self = BotSelf(platform="qq", user_id=settings.onebot_self_id)
        bot.add_gateway(
            OneBot11Gateway(
                bot,
                ingress=[
                    ForwardWebSocket(
                        settings.onebot_ws_url,
                        role="universal",
                        self_=onebot_self,
                    )
                ],
                action=WebSocketAction(),
                access_token=settings.onebot_access_token,
            )
        )
        if settings.report_group_id:
            bot.on_cron(
                "0 0,8-23 * * *",
                self_=onebot_self,
                gateway=OneBot11Gateway,
            )(report)
    if settings.telegram_bot_token.get_secret_value():
        bot.add_gateway(
            TelegramGateway(
                bot,
                token=settings.telegram_bot_token,
                http_pool=http_pool,
            )
        )
    if settings.discord_bot_token.get_secret_value():
        bot.add_gateway(
            DiscordGateway(
                bot,
                token=settings.discord_bot_token,
                intents=DiscordIntent(settings.discord_intents),
                http_pool=http_pool,
                websocket_connector=partial(
                    connect_websocket,
                    proxy=settings.http_proxy,
                    max_size=None,
                ),
            )
        )

    for instance in (
        settings,
        LstClient(),
        HitokotoClient(http_pool=http_pool),
        KleiClient(
            access_token=settings.klei_access_token,
            http_pool=http_pool,
        ),
        question_agent,
    ):
        bot.container.add_instance(instance)

    for router in (general_router, question_router, rooms_router):
        bot.add_router(router)

    return bot


async def run(settings: Settings) -> None:
    async with (
        AsyncProxyManager(settings.http_proxy) as http_pool,
        AsyncClient(
            proxy=settings.http_proxy or None,
            timeout=REQUEST_TIMEOUT,
        ) as model_http_client,
    ):
        question_agent = build_question_agent(
            settings,
            http_client=model_http_client,
        )
        bot = build_bot(
            settings,
            http_pool=http_pool,
            question_agent=question_agent,
        )

        async with question_agent, bot:
            await asyncio.Event().wait()


def main() -> None:
    settings = Settings()
    for name in ("httpcore", "urllib3_future", "websockets", "mcp"):
        logging.getLogger(name).setLevel(logging.INFO)
    with (
        redirected_logging(),
        ColorizedStderrHandler(level=settings.log_level).applicationbound(),
    ):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
