from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Self

import pytest
from bot import Bot
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from lst_bot.agent import DstQuestionAgent
from lst_bot.main import Application


@dataclass
class Result:
    output: str


class CountingAgent:
    def __init__(self) -> None:
        self.enters = 0
        self.exits = 0
        self.questions: list[str] = []

    async def __aenter__(self) -> Self:
        self.enters += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback
        self.exits += 1

    async def run(self, question: str) -> Result:
        self.questions.append(question)
        return Result(output=f"回答：{question}")


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
    backend = CountingAgent()
    agent = DstQuestionAgent(
        openrouter_api_key=SecretStr("test"),
        dosu_mcp_endpoint="http://invalid.test/mcp",
        dosu_api_key=SecretStr("test"),
        agent=backend,
    )

    async with Application(Bot(), (agent,)):
        assert await agent.answer("问题一") == "回答：问题一"
        assert await agent.answer("问题二") == "回答：问题二"

    assert backend.enters == 1
    assert backend.exits == 1
    assert backend.questions == ["问题一", "问题二"]


async def test_agent_rejects_answers_outside_its_lifecycle() -> None:
    agent = make_agent()

    with pytest.raises(RuntimeError, match="started"):
        await agent.answer("问题")

    async with agent:
        pass

    with pytest.raises(RuntimeError, match="started"):
        await agent.answer("问题")
