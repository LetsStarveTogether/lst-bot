from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import timedelta
from types import TracebackType
from zoneinfo import ZoneInfo

import pytest
from bot import Bot
from pydantic import SecretStr

import lst_bot.main as main_module
from lst_bot.main import Application, build_application
from lst_bot.settings import Settings


class RecordingResource(AbstractAsyncContextManager[None]):
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    async def __aenter__(self) -> None:
        self.events.append(f"{self.name}:start")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback
        self.events.append(f"{self.name}:close")


class FailingResource(AbstractAsyncContextManager[None]):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> None:
        self.events.append("failing:start")
        msg = "startup failed"
        raise RuntimeError(msg)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback


async def test_application_starts_services_before_bot_and_closes_bot_first() -> None:
    events: list[str] = []
    bot = Bot()
    bot.on_start(lambda: events.append("bot:start"))
    bot.on_close(lambda: events.append("bot:close"))
    first = RecordingResource("first", events)
    second = RecordingResource("second", events)

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
    first = RecordingResource("first", events)
    application = Application(Bot(), (first, FailingResource(events)))

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
        openrouter_api_key=SecretStr("test"),
        dosu_mcp_endpoint="http://invalid.test/mcp",
    )

    application = build_application(settings)

    assert application.bot.cmd_prefixes == ("!",)
    assert application.bot.dispatch_timeout == timeout
    assert application.bot.scheduler_timezone == timezone
    assert application.bot.container.resolve(Settings) is settings


def test_importing_main_creates_no_runtime_resources() -> None:
    assert not hasattr(main_module, "settings")
    assert not hasattr(main_module, "bot")
    assert not hasattr(main_module, "gateway")


def test_main_loads_settings_inside_logging_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    settings = Settings(_env_file=None, onebot_self_id="10000")

    class StubApplication:
        async def run(self) -> None:
            events.append("run")

    @contextmanager
    def logging_context(value: Settings) -> Iterator[None]:
        assert value is settings
        events.append("logging:start")
        try:
            yield
        finally:
            events.append("logging:close")

    def make_application(value: Settings) -> StubApplication:
        assert value is settings
        return StubApplication()

    monkeypatch.setattr(main_module, "Settings", lambda: settings)
    monkeypatch.setattr(main_module, "configure_logging", logging_context)
    monkeypatch.setattr(main_module, "build_application", make_application)
    monkeypatch.setattr(main_module.uvloop, "run", asyncio.run)

    main_module.main()

    assert events == ["logging:start", "run", "logging:close"]
