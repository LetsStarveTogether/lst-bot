from datetime import timedelta
from logging import ERROR, getLogger
from typing import Never
from unittest.mock import Mock, call
from zoneinfo import ZoneInfo

import pytest
from bot import BotSelf
from bot.gateways.discord import DiscordGateway
from bot.gateways.onebot11 import OneBot11Gateway
from bot.gateways.telegram import TelegramGateway
from hitokoto import HitokotoClient
from httpx import AsyncClient
from klei import KleiClient
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from urllib3_future import AsyncPoolManager

from lst_bot.agent import mcp_http_client
from lst_bot.main import build_bot, main, run
from lst_bot.settings import Settings


async def test_run_closes_model_client_when_agent_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[AsyncClient] = []

    def fail(_settings: Settings, *, http_client: AsyncClient) -> Never:
        clients.append(http_client)
        msg = "build failed"
        raise RuntimeError(msg)

    monkeypatch.setenv("ALL_PROXY", "invalid://proxy")
    monkeypatch.setattr("lst_bot.main.build_question_agent", fail)

    with pytest.raises(RuntimeError, match="build failed"):
        await run(Settings(_env_file=None, http_proxy=""))

    assert clients[0].is_closed


async def test_mcp_client_refuses_redirects_that_could_leak_api_key() -> None:
    async with mcp_http_client(None, follow_redirects=True) as client:
        assert client.follow_redirects is False


def test_main_never_lowers_dependency_log_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    monkeypatch.setattr("lst_bot.main.Settings", lambda: Mock(log_level=ERROR))
    monkeypatch.setattr("lst_bot.main.logging.basicConfig", Mock())
    monkeypatch.setattr(
        "lst_bot.main.logging.getLogger",
        lambda name=None: logger if name else getLogger(),
    )
    monkeypatch.setattr("lst_bot.main.run", lambda _: None)
    monkeypatch.setattr("lst_bot.main.asyncio.run", lambda _: None)

    main()

    assert logger.setLevel.call_args_list == [call(ERROR)] * 5


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
    http_pool = AsyncPoolManager()
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
    assert telegram.http_pool is hitokoto.http_pool is klei.http_pool is http_pool
    assert discord.intents == settings.discord_intents
    (report_job,) = bot.scheduler.jobs
    assert report_job.gateway_type is OneBot11Gateway
    assert report_job.self_ == BotSelf(platform="qq", user_id="10000")


def test_build_bot_skips_unconfigured_gateways_and_report() -> None:
    settings = Settings(_env_file=None)
    bot = build_bot(
        settings,
        http_pool=AsyncPoolManager(),
        question_agent=Agent(TestModel()),
    )

    for gateway_type in (OneBot11Gateway, TelegramGateway, DiscordGateway):
        with pytest.raises(LookupError, match="No gateway"):
            bot.resolve_gateway(gateway_type)
    assert bot.scheduler.jobs == ()
