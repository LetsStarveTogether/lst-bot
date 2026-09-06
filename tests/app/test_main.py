from asyncio import CancelledError, Event, Task, create_task, gather
from collections.abc import AsyncIterator
from datetime import timedelta
from logging import DEBUG, ERROR, INFO, WARNING, getLogger
from pathlib import Path
from typing import Never
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from bot import Bot, BotSelf
from bot.gateways.discord import DiscordGateway
from bot.gateways.onebot11 import OneBot11Gateway
from bot.gateways.telegram import TelegramGateway
from hitokoto import HitokotoClient
from httpx2 import AsyncByteStream, AsyncClient, MockTransport, Response, Timeout
from klei import KleiClient
from lst import LstClient
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from lst_bot.agent import REQUEST_TIMEOUT
from lst_bot.main import build_bot, main, run
from lst_bot.settings import Settings


async def test_run_closes_http_client_when_agent_build_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[AsyncClient] = []
    create_client = Mock(wraps=AsyncClient)

    def fail(_settings: Settings, *, http_client: AsyncClient) -> Never:
        clients.append(http_client)
        msg = "build failed"
        raise RuntimeError(msg)

    monkeypatch.setenv("ALL_PROXY", "invalid://proxy")
    monkeypatch.setattr("lst_bot.main.AsyncClient", create_client)
    monkeypatch.setattr("lst_bot.main.build_question_agent", fail)

    with pytest.raises(RuntimeError, match="build failed"):
        await run(Settings(_env_file=None, http_proxy=""))

    assert clients[0].is_closed
    create_client.assert_called_once_with(
        proxy=None,
        timeout=Timeout(REQUEST_TIMEOUT, connect=5),
        http2=True,
        trust_env=False,
        follow_redirects=False,
    )


async def test_run_drains_detached_refresh_before_closing_http_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requested = Event()
    waiter: list[Task] = []
    client_closed_during_refresh_cleanup: list[bool] = []

    class HangingStream(AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            requested.set()
            await Event().wait()
            yield b""

        async def aclose(self) -> None:
            client_closed_during_refresh_cleanup.append(http_client.is_closed)

    http_client = AsyncClient(
        transport=MockTransport(lambda _: Response(200, stream=HangingStream())),
        trust_env=False,
    )

    def build(
        _settings: Settings,
        *,
        hitokoto_client: HitokotoClient,
        **_: object,
    ) -> Bot:
        waiter.append(create_task(hitokoto_client.get_hitokoto()))
        return Bot()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lst_bot.main.AsyncClient", lambda **_: http_client)
    monkeypatch.setattr("lst_bot.main.build_bot", build)
    monkeypatch.setattr(
        "lst_bot.main.build_question_agent",
        lambda *_args, **_kwargs: Agent(TestModel()),
    )

    running = create_task(run(Settings(_env_file=None)))
    try:
        await requested.wait()
        waiter[0].cancel()
        with pytest.raises(CancelledError):
            await waiter[0]
        assert not client_closed_during_refresh_cleanup
        running.cancel()
        with pytest.raises(CancelledError):
            await running
        assert client_closed_during_refresh_cleanup == [False]
        assert http_client.is_closed
    finally:
        running.cancel()
        for task in waiter:
            task.cancel()
        await gather(running, *waiter, return_exceptions=True)


@pytest.mark.parametrize(("level", "expected"), [(DEBUG, INFO), (ERROR, ERROR)])
def test_main_never_lowers_dependency_log_level(
    monkeypatch: pytest.MonkeyPatch,
    level: int,
    expected: int,
) -> None:
    loggers = {
        name: Mock() for name in ("httpx2", "httpcore2", "websockets", "mcp", "fastmcp")
    }
    monkeypatch.setattr("lst_bot.main.Settings", lambda: Mock(log_level=level))
    monkeypatch.setattr("lst_bot.main.logging.basicConfig", Mock())
    monkeypatch.setattr(
        "lst_bot.main.logging.getLogger",
        lambda name=None: getLogger() if name is None else loggers[name],
    )
    monkeypatch.setattr("lst_bot.main.run", lambda _: None)
    monkeypatch.setattr("lst_bot.main.asyncio.run", lambda _: None)

    main()

    for name, logger in loggers.items():
        logger.setLevel.assert_called_once_with(
            max(expected, WARNING) if name == "httpx2" else expected
        )


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
    http_client = AsyncClient(trust_env=False)
    hitokoto_client = HitokotoClient(http_client=http_client)
    question_agent = Agent(TestModel())

    bot = build_bot(
        settings,
        http_client=http_client,
        hitokoto_client=hitokoto_client,
        question_agent=question_agent,
    )

    assert bot.cmd_prefixes == ("!",)
    assert bot.dispatch_timeout == timeout
    assert bot.scheduler.jobs[0].timezone == timezone
    assert bot.admin_ids == {"qq": frozenset({"owner"})}
    assert bot.dependencies[Settings] is settings
    assert isinstance(bot.dependencies[LstClient], LstClient)
    assert bot.dependencies[Agent] is question_agent
    assert {route.name for route in bot.routes} == {
        "一言",
        "问",
        "房间列表",
        "房间回档",
        "房间存档",
        "房间重启",
        "房间重置",
        "最新版本",
    }
    hitokoto = bot.dependencies[HitokotoClient]
    klei = bot.dependencies[KleiClient]
    assert isinstance(hitokoto, HitokotoClient)
    assert hitokoto is hitokoto_client
    assert isinstance(klei, KleiClient)
    telegram = bot.resolve_gateway(TelegramGateway)
    discord = bot.resolve_gateway(DiscordGateway)
    bot.resolve_gateway(OneBot11Gateway)
    assert telegram.http_client is discord.http_client
    assert (
        telegram.http_client is hitokoto.http_client is klei.http_client is http_client
    )
    assert discord.intents == settings.discord_intents
    (report_job,) = bot.scheduler.jobs
    assert report_job.gateway_type is OneBot11Gateway
    assert report_job.self_ == BotSelf(platform="qq", user_id="10000")


def test_build_bot_skips_unconfigured_gateways_and_report() -> None:
    settings = Settings(_env_file=None)
    http_client = AsyncClient(trust_env=False)
    bot = build_bot(
        settings,
        http_client=http_client,
        hitokoto_client=HitokotoClient(http_client=http_client),
        question_agent=Agent(TestModel()),
    )

    for gateway_type in (OneBot11Gateway, TelegramGateway, DiscordGateway):
        with pytest.raises(LookupError, match="No gateway"):
            bot.resolve_gateway(gateway_type)
    assert bot.scheduler.jobs == ()
