from __future__ import annotations

import uvloop
from bot import Bot, Event, Injected
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
from .settings import settings

logger = Logger(__name__)


bot = Bot(admin_ids=settings.bot_admin)
gateway = OneBot11Gateway(
    bot,
    ingress=[ForwardWebSocket(settings.onebot_ws_url)],
    action=WebSocketAction(),
    access_token=settings.onebot_access_token,
)
bot.add_gateway(gateway)
bot.container.add_instance(LstClient())
bot.container.add_instance(
    HitokotoClient(
        http_pool=AsyncProxyManager(settings.http_proxy),
    )
)
bot.container.add_instance(
    KleiClient(
        access_token=settings.klei_access_token,
        http_pool=AsyncProxyManager(settings.http_proxy),
    ),
)
bot.container.add_instance(
    DstQuestionAgent(
        openrouter_api_key=settings.openrouter_api_key,
        dosu_mcp_endpoint=settings.dosu_mcp_endpoint,
        dosu_api_key=settings.dosu_api_key,
        http_proxy=settings.http_proxy,
    ),
)

for router in (general_router, question_router, rooms_router):
    bot.add_router(router)
register_crons(bot)


@bot.on_event()
def log_event(event: Injected[Event]) -> None:
    if __debug__:
        logger.trace(
            "receive event : {event}",
            event=event,
        )


def main() -> None:
    uvloop.run(bot.run())


if __name__ == "__main__":
    main()
