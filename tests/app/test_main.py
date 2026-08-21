from datetime import timedelta
from typing import Never
from zoneinfo import ZoneInfo

import pytest
from bot import BotSelf
from bot.gateways.discord import DiscordGateway, DiscordIntent
from bot.gateways.onebot11 import OneBot11Gateway
from bot.gateways.telegram import TelegramGateway
from hitokoto import HitokotoClient
from httpx import AsyncClient
from klei import KleiClient
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from urllib3_future import AsyncProxyManager

from lst_bot.main import build_bot, run
from lst_bot.settings import Settings


async def test_run_closes_model_client_when_agent_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[AsyncClient] = []

    def fail(_settings: Settings, *, http_client: AsyncClient) -> Never:
        clients.append(http_client)
        msg = "build failed"
        raise RuntimeError(msg)

    monkeypatch.setattr("lst_bot.main.build_question_agent", fail)

    with pytest.raises(RuntimeError, match="build failed"):
        await run(Settings(_env_file=None))

    assert clients[0].is_closed


def test_build_bot_registers_runtime_settings() -> None:
    timeout = timedelta(minutes=5)
    timezone = ZoneInfo("UTC")
    settings = Settings(
        _env_file=None,
        onebot_self_id="10000",
        onebot_ws_url="ws://127.0.0.1:6700",
        bot_cmd_prefixes=("!",),
        bot_timeout=timeout,
        bot_timezone=timezone,
        bot_admin={"qq": {"owner"}},
        telegram_bot_token=SecretStr("123:test"),
        discord_bot_token=SecretStr("discord.test"),
        report_group_id="20000",
    )
    http_pool = AsyncProxyManager(settings.http_proxy)
    question_agent = Agent(TestModel())

    bot = build_bot(
        settings,
        http_pool=http_pool,
        question_agent=question_agent,
    )

    assert bot.cmd_prefixes == ("!",)
    assert bot.dispatch_timeout == timeout
    assert bot.scheduler.jobs[0].timezone == timezone
    assert bot.admin_ids == {"qq": frozenset({"owner"})}
    assert bot.container.resolve(Settings) is settings
    assert bot.container.resolve(Agent) is question_agent
    hitokoto = bot.container.resolve(HitokotoClient)
    klei = bot.container.resolve(KleiClient)
    telegram = bot.resolve_gateway(TelegramGateway)
    discord = bot.resolve_gateway(DiscordGateway)
    assert isinstance(telegram, TelegramGateway)
    assert isinstance(discord, DiscordGateway)
    bot.resolve_gateway(OneBot11Gateway)
    assert telegram.http_pool is discord.http_pool
    assert telegram.http_pool is hitokoto.http_pool is klei.http_pool
    assert telegram.http_pool is http_pool
    assert discord.intents == DiscordIntent(settings.discord_intents)
    (report_job,) = bot.scheduler.jobs
    assert report_job.gateway_type is OneBot11Gateway
    assert report_job.self_ == BotSelf(platform="qq", user_id="10000")


def test_build_bot_skips_unconfigured_gateways_and_report() -> None:
    settings = Settings(_env_file=None)
    bot = build_bot(
        settings,
        http_pool=AsyncProxyManager(settings.http_proxy),
        question_agent=Agent(TestModel()),
    )

    for gateway_type in (OneBot11Gateway, TelegramGateway, DiscordGateway):
        with pytest.raises(LookupError, match="No gateway"):
            bot.resolve_gateway(gateway_type)
    assert bot.scheduler.jobs == ()
