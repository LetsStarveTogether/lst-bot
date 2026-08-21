import pytest
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from lst_bot.agent import DstQuestionAgent


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
        with pytest.raises(RuntimeError, match="restarted"):
            async with agent:
                pytest.fail("nested lifecycle should fail")
        assert await agent.answer("问题二") == "测试回答"


async def test_agent_rejects_answers_outside_its_lifecycle() -> None:
    agent = make_agent()

    with pytest.raises(RuntimeError, match="started"):
        await agent.answer("问题")

    async with agent:
        pass

    with pytest.raises(RuntimeError, match="started"):
        await agent.answer("问题")
