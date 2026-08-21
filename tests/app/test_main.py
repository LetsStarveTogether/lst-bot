from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from bot import Bot, BotSelf
from bot.gateways.discord import DiscordGateway, DiscordIntent
from bot.gateways.onebot11 import OneBot11Gateway
from bot.gateways.telegram import TelegramGateway
from hitokoto import HitokotoClient
from klei import KleiClient
from pydantic import SecretStr

from lst_bot.main import Application, build_application
from lst_bot.settings import Settings


@asynccontextmanager
async def resource(
    name: str,
    events: list[str],
    *,
    fail: bool = False,
) -> AsyncIterator[None]:
    events.append(f"{name}:start")
    if fail:
        msg = "startup failed"
        raise RuntimeError(msg)
    try:
        yield
    finally:
        events.append(f"{name}:close")


async def test_application_starts_services_before_bot_and_closes_bot_first() -> None:
    events: list[str] = []
    bot = Bot()
    bot.on_start(lambda: events.append("bot:start"))
    bot.on_close(lambda: events.append("bot:close"))
    first = resource("first", events)
    second = resource("second", events)

    async with Application(bot, (first, second)):
        assert events == ["first:start", "second:start", "bot:start"]

    assert events == [
        "first:start",
        "second:start",
        "bot:start",
        "bot:close",
        "second:close",
        "first:close",
    ]


async def test_application_rolls_back_started_services() -> None:
    events: list[str] = []
    first = resource("first", events)
    application = Application(Bot(), (first, resource("failing", events, fail=True)))

    with pytest.raises(RuntimeError, match="startup failed"):
        async with application:
            pytest.fail("startup should fail")

    assert events == ["first:start", "failing:start", "first:close"]


def test_build_application_registers_runtime_settings() -> None:
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
        openrouter_api_key=SecretStr("test"),
        dosu_mcp_endpoint="http://invalid.test/mcp",
        report_group_id="20000",
    )

    application = build_application(settings)

    assert application.bot.cmd_prefixes == ("!",)
    assert application.bot.dispatch_timeout == timeout
    assert application.bot.scheduler_timezone == timezone
    assert application.bot.admin_ids == {"qq": frozenset({"owner"})}
    assert application.bot.container.resolve(Settings) is settings
    hitokoto = application.bot.container.resolve(HitokotoClient)
    klei = application.bot.container.resolve(KleiClient)
    telegram = application.bot.resolve_gateway(TelegramGateway)
    discord = application.bot.resolve_gateway(DiscordGateway)
    assert isinstance(telegram, TelegramGateway)
    assert isinstance(discord, DiscordGateway)
    application.bot.resolve_gateway(OneBot11Gateway)
    assert telegram.http_pool is discord.http_pool
    assert telegram.http_pool is hitokoto.http_pool is klei.http_pool
    assert telegram.http_pool in application.resources
    assert discord.intents == DiscordIntent(settings.discord_intents)
    (report_job,) = application.bot.scheduler.jobs
    assert report_job.gateway_type is OneBot11Gateway
    assert report_job.self_ == BotSelf(platform="qq", user_id="10000")


def test_build_application_skips_unconfigured_gateways_and_report() -> None:
    application = build_application(
        Settings(
            _env_file=None,
            openrouter_api_key=SecretStr("test"),
            dosu_mcp_endpoint="http://invalid.test/mcp",
        )
    )

    for gateway_type in (OneBot11Gateway, TelegramGateway, DiscordGateway):
        with pytest.raises(LookupError, match="No gateway"):
            application.bot.resolve_gateway(gateway_type)
    assert application.bot.scheduler.jobs == ()
