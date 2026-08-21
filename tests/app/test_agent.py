from unittest.mock import AsyncMock, call

import pytest
from bot import Bot
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from lst_bot.agent import DstQuestionAgent
from lst_bot.main import Application


def make_agent() -> DstQuestionAgent:
    return DstQuestionAgent(
        openrouter_api_key=SecretStr("test"),
        dosu_mcp_endpoint="http://invalid.test/mcp",
        dosu_api_key=SecretStr("test"),
        agent=Agent(TestModel(custom_output_text="测试回答")),
    )


async def test_agent_reuses_one_started_pydantic_agent() -> None:
    agent = make_agent()

    async with agent:
        assert await agent.answer("问题一") == "测试回答"
        assert await agent.answer("问题二") == "测试回答"


async def test_application_owns_one_reused_agent_lifecycle() -> None:
    backend = AsyncMock()
    backend.run.return_value.output = "回答"
    agent = DstQuestionAgent(
        openrouter_api_key=SecretStr("test"),
        dosu_mcp_endpoint="http://invalid.test/mcp",
        dosu_api_key=SecretStr("test"),
        agent=backend,
    )

    async with Application(Bot(), (agent,)):
        assert await agent.answer("问题一") == "回答"
        assert await agent.answer("问题二") == "回答"

    backend.__aenter__.assert_awaited_once()
    backend.__aexit__.assert_awaited_once()
    assert backend.run.await_args_list == [call("问题一"), call("问题二")]


async def test_agent_rejects_answers_outside_its_lifecycle() -> None:
    agent = make_agent()

    with pytest.raises(RuntimeError, match="started"):
        await agent.answer("问题")

    async with agent:
        pass

    with pytest.raises(RuntimeError, match="started"):
        await agent.answer("问题")
