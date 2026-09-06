import asyncio
import logging
from functools import partial

from bot import Bot, BotSelf
from bot.gateways.base import connect_websocket
from bot.gateways.discord import DiscordGateway
from bot.gateways.onebot11 import ForwardWebSocket, OneBot11Gateway, WebSocketAction
from bot.gateways.telegram import TelegramGateway
from hitokoto import HitokotoClient
from httpx2 import AsyncClient, Timeout
from klei import KleiClient
from lst import LstClient
from pydantic_ai import Agent

from .agent import REQUEST_TIMEOUT, build_question_agent
from .general import report
from .general import router as general_router
from .question import router as question_router
from .rooms import router as rooms_router
from .settings import Settings


def build_bot(
    settings: Settings,
    *,
    http_client: AsyncClient,
    hitokoto_client: HitokotoClient,
    question_agent: Agent,
) -> Bot:
    bot = Bot(
        admin_ids=settings.bot_admin,
        cmd_prefixes=settings.bot_cmd_prefixes,
        dispatch_timeout=settings.bot_timeout,
        dependencies={
            Settings: settings,
            LstClient: LstClient(),
            HitokotoClient: hitokoto_client,
            KleiClient: KleiClient(
                access_token=settings.klei_access_token,
                http_client=http_client,
            ),
            Agent: question_agent,
        },
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
            bot.scheduler.on_cron(
                "0 0,8-23 * * *",
                self_=onebot_self,
                gateway=OneBot11Gateway,
            )(report)
    if settings.telegram_bot_token:
        bot.add_gateway(
            TelegramGateway(
                bot,
                token=settings.telegram_bot_token,
                http_client=http_client,
            )
        )
    if settings.discord_bot_token:
        bot.add_gateway(
            DiscordGateway(
                bot,
                token=settings.discord_bot_token,
                intents=settings.discord_intents,
                http_client=http_client,
                websocket_connector=partial(
                    connect_websocket,
                    proxy=settings.proxy_url,
                    max_size=None,
                ),
            )
        )

    for router in (general_router, question_router, rooms_router):
        bot.add_router(router)

    return bot


async def run(settings: Settings) -> None:
    async with (
        AsyncClient(
            proxy=settings.proxy_url,
            timeout=Timeout(REQUEST_TIMEOUT, connect=5),
            http2=True,
            trust_env=False,
            follow_redirects=False,
        ) as http_client,
        HitokotoClient(http_client=http_client) as hitokoto_client,
        build_question_agent(
            settings,
            http_client=http_client,
        ) as question_agent,
    ):
        bot = build_bot(
            settings,
            http_client=http_client,
            hitokoto_client=hitokoto_client,
            question_agent=question_agent,
        )

        async with bot:
            await asyncio.Event().wait()


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, force=True)
    # HTTPX request URLs contain Telegram bot credentials.
    logging.getLogger("httpx2").setLevel(max(settings.log_level, logging.WARNING))
    for name in ("httpcore2", "websockets", "mcp", "fastmcp"):
        logging.getLogger(name).setLevel(max(settings.log_level, logging.INFO))
    asyncio.run(run(settings))
