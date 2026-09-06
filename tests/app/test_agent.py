from json import loads
from typing import Any
from unittest.mock import Mock, call

import pytest
from httpx2 import AsyncClient, MockTransport, Request, Response, Timeout
from pydantic_ai import ModelRetry, RunContext, WebSearchTool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from lst_bot.agent import build_question_agent
from lst_bot.settings import Settings

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


class MCPServer:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clients: list[AsyncClient] = []
        self.methods: list[str] = []
        self.tool_names: tuple[str, ...] = ("read_knowledge", "write_knowledge")
        self.reply: dict[str, Any] = {
            "result": {"content": [{"type": "text", "text": "answer"}]}
        }
        self.create_client = Mock(side_effect=self.http_client)
        monkeypatch.setattr("lst_bot.agent.AsyncClient", self.create_client)

    def http_client(self, **kwargs: Any) -> AsyncClient:
        # Record proxy configuration while keeping the test on MockTransport.
        kwargs.pop("proxy")
        client = AsyncClient(transport=MockTransport(self.respond), **kwargs)
        self.clients.append(client)
        return client

    def respond(self, request: Request) -> Response:
        assert request.headers["X-Dosu-API-Key"] == "test"
        assert request.url == "https://example.com/mcp"
        payload = loads(request.content)
        method = payload["method"]
        self.methods.append(method)
        envelope = {"jsonrpc": "2.0", "id": payload.get("id")}
        if method == "server/discover":
            return Response(
                200,
                json={
                    **envelope,
                    "error": {"code": -32601, "message": "Unknown method"},
                },
            )
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "dosu-test", "version": "1"},
                "instructions": "Use the knowledge references.",
            }
        elif method == "notifications/initialized":
            return Response(202)
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "Search DST source code.",
                        "inputSchema": INPUT_SCHEMA,
                    }
                    for name in self.tool_names
                ]
            }
        else:
            assert method == "tools/call"
            assert payload["params"]["name"] == "read_knowledge"
            assert payload["params"]["arguments"] == {"query": "prefab"}
            return Response(200, json={**envelope, **self.reply})
        return Response(200, json={**envelope, "result": result})


async def test_openrouter_uses_injected_httpx2_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = MCPServer(monkeypatch)
    requests: list[Request] = []

    def respond(request: Request) -> Response:
        requests.append(request)
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test"
        assert "X-Dosu-API-Key" not in request.headers
        payload = loads(request.content)
        assert payload["model"] == "deepseek/deepseek-v4-pro-0813"
        assert {tool["type"] for tool in payload["tools"]} == {
            "function",
            "openrouter:web_search",
        }
        return Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "provider": "DeepSeek",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "model answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    async with AsyncClient(
        transport=MockTransport(respond), trust_env=False
    ) as http_client:
        async with build_question_agent(
            Settings(_env_file=None), http_client=http_client
        ) as agent:
            assert (await agent.run("Check the prefab")).output == "model answer"
        assert not http_client.is_closed
        assert server.clients[0].is_closed
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("proxy", "expected_proxy"),
    [(None, None), ("http://proxy.example", "http://proxy.example/")],
)
async def test_mcp_sessions_configure_http_filter_tools_and_close_clients(
    monkeypatch: pytest.MonkeyPatch,
    proxy: str | None,
    expected_proxy: str | None,
) -> None:
    server = MCPServer(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", "invalid://proxy")

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        (tool,) = info.function_tools
        assert tool.name == "read_knowledge"
        assert tool.description == "Search DST source code."
        assert tool.parameters_json_schema == INPUT_SCHEMA
        assert any(
            isinstance(tool, WebSearchTool)
            for tool in info.model_request_parameters.native_tools
        )
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("read_knowledge", {"query": "prefab"})]
            )
        return ModelResponse(parts=[TextPart("answer received")])

    async with AsyncClient(trust_env=False) as model_client:
        for _ in range(2):
            async with build_question_agent(
                Settings(_env_file=None, http_proxy=proxy), http_client=model_client
            ) as agent:
                result = await agent.run(
                    "Check the prefab", model=FunctionModel(respond)
                )
                assert result.output == "answer received"
                assert any(
                    isinstance(part, ToolReturnPart) and part.content == "answer"
                    for message in result.all_messages()
                    for part in message.parts
                )
            assert server.clients[-1].is_closed
            assert not model_client.is_closed

    assert len(server.clients) == 2
    assert server.clients[0] is not server.clients[1]
    assert (
        server.create_client.call_args_list
        == [
            call(
                headers={"X-Dosu-API-Key": "test"},
                proxy=expected_proxy,
                timeout=Timeout(600, connect=5),
                http2=True,
                trust_env=False,
                follow_redirects=False,
            )
        ]
        * 2
    )
    assert (
        server.methods
        == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/call",
        ]
        * 2
    )


@pytest.mark.parametrize(
    ("reply", "expected", "error"),
    [
        pytest.param(
            {"result": {"content": [], "structuredContent": {}}},
            {},
            None,
            id="empty-structured",
        ),
        pytest.param(
            {
                "result": {
                    "content": [{"type": "text", "text": "old copy"}],
                    "structuredContent": {"answer": "structured"},
                }
            },
            {"answer": "structured"},
            None,
            id="structured-precedes-text",
        ),
        pytest.param(
            {
                "result": {
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ]
                }
            },
            ["first", "second"],
            None,
            id="multiple-text-blocks",
        ),
        pytest.param(
            {
                "result": {
                    "content": [{"type": "text", "text": "generic error"}],
                    "structuredContent": {"detail": "specific error"},
                    "isError": True,
                }
            },
            None,
            "specific error",
            id="structured-tool-error",
        ),
        pytest.param(
            {"result": {"content": [], "isError": True}},
            None,
            "read_knowledge failed",
            id="empty-tool-error",
        ),
        pytest.param(
            {"error": {"code": -32602, "message": "Invalid query"}},
            None,
            "Invalid query",
            id="protocol-error",
        ),
    ],
)
async def test_knowledge_tool_preserves_results_and_retries_errors(
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any],
    expected: object,
    error: str | None,
) -> None:
    server = MCPServer(monkeypatch)
    server.reply = reply
    context = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    async with (
        AsyncClient(trust_env=False) as model_client,
        build_question_agent(
            Settings(_env_file=None), http_client=model_client
        ) as agent,
    ):
        (toolset,) = agent.toolsets
        tools = await toolset.get_tools(context)
        invocation = toolset.call_tool(
            "read_knowledge", {"query": "prefab"}, context, tools["read_knowledge"]
        )
        if error is None:
            assert await invocation == expected
        else:
            with pytest.raises(ModelRetry, match=error):
                await invocation
    assert all(client.is_closed for client in server.clients)


@pytest.mark.parametrize("arguments", [{}, {"query": 123}])
async def test_knowledge_arguments_are_validated_before_request(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
) -> None:
    server = MCPServer(monkeypatch)
    context = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    async with (
        AsyncClient(trust_env=False) as model_client,
        build_question_agent(
            Settings(_env_file=None), http_client=model_client
        ) as agent,
    ):
        (toolset,) = agent.toolsets
        tools = await toolset.get_tools(context)
        with pytest.raises(ModelRetry):
            await toolset.call_tool(
                "read_knowledge", arguments, context, tools["read_knowledge"]
            )
        assert "tools/call" not in server.methods


async def test_missing_knowledge_tool_fails_startup_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = MCPServer(monkeypatch)
    server.tool_names = ("write_knowledge",)
    async with AsyncClient(trust_env=False) as model_client:
        with pytest.raises(RuntimeError, match="does not provide read_knowledge"):
            async with build_question_agent(
                Settings(_env_file=None), http_client=model_client
            ):
                pytest.fail("An agent without the knowledge tool must not start")
        assert not model_client.is_closed
    assert len(server.clients) == 1
    assert server.clients[0].is_closed
