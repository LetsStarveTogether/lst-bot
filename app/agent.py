from __future__ import annotations

from typing import Any, Final

from fastmcp.client import Client as FastMCPClient
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from fastmcp.mcp_config import infer_transport_type_from_url
from httpx import AsyncClient
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.providers.google import GoogleProvider

GEMINI_MODEL: Final = "gemini-3.5-flash"
DOSU_API_KEY_HEADER: Final = "X-Dosu-API-Key"
REQUEST_TIMEOUT: Final = 600

DST_AGENT_INSTRUCTIONS: Final = """\
你是《饥荒联机版》（Don't Starve Together）的问答助手。
你有一套 Dosu MCP 工具，其主要能力是根据 DST lua 脚本代码进行初步查询。
你也可以使用 Gemini 内置搜索补充公开网页信息。

流程必须严格遵守：
1. 把用户问题补全为完整、清晰的 DST 语境问题（最好明确包含 lua 代码标识符）。
2. 调用 Dosu MCP 的 ask 工具，用补全后的问题寻求答案。
3. 如果 ask 工具结果不足，或问题需要最新公开信息，再使用 Gemini 内置搜索补充核对。
4. 基于工具结果给出最终回复；如果工具结果仍不足，明确说明不确定。

回答要求：
- 简体中文，不超过 500 字，不使用 markdown 标记，用基本的空格和换行排版。
- 语气友好接地气，但不要客套和招呼。
- 不编造版本机制、角色数值、代码或服务器配置。
"""

USER_PROMPT_TEMPLATE: Final = """\
用户原始问题：
{question}

请先根据《饥荒联机版》的基础背景补全这个问题，\
再调用 ask 工具获取答案，最后回复用户。
"""


class DstQuestionAgent:
    def __init__(
        self,
        *,
        gemini_api_key: SecretStr,
        dosu_mcp_endpoint: str,
        dosu_api_key: SecretStr,
        http_proxy: str | None = None,
    ) -> None:
        self._gemini_api_key = gemini_api_key
        self._dosu_mcp_endpoint = dosu_mcp_endpoint
        self._dosu_api_key = dosu_api_key
        self._http_proxy = http_proxy

    async def answer(self, question: str) -> str:
        proxy = self._http_proxy or None
        async with AsyncClient(
            proxy=proxy,
            timeout=REQUEST_TIMEOUT,
        ) as google_http_client:
            model = GoogleModel(
                GEMINI_MODEL,
                provider=GoogleProvider(
                    api_key=self._gemini_api_key.get_secret_value(),
                    http_client=google_http_client,
                ),
            )
            dosu_client = FastMCPClient(
                self._dosu_transport(
                    api_key=self._dosu_api_key.get_secret_value(),
                    proxy=proxy,
                ),
                timeout=REQUEST_TIMEOUT,
                init_timeout=REQUEST_TIMEOUT,
            )
            dosu_tools = MCPToolset(dosu_client)
            agent = Agent(
                model,
                instructions=DST_AGENT_INSTRUCTIONS,
                toolsets=[dosu_tools],
                capabilities=[NativeTool(WebSearchTool())],
            )
            async with agent:
                result = await agent.run(
                    USER_PROMPT_TEMPLATE.format(question=question),
                )

        return result.output

    def _dosu_transport(
        self,
        *,
        api_key: str,
        proxy: str | None,
    ) -> SSETransport | StreamableHttpTransport:
        httpx_client_factory = self._mcp_http_client_factory(
            api_key=api_key,
            proxy=proxy,
        )
        if infer_transport_type_from_url(self._dosu_mcp_endpoint) == "sse":
            return SSETransport(
                self._dosu_mcp_endpoint,
                httpx_client_factory=httpx_client_factory,
            )
        return StreamableHttpTransport(
            self._dosu_mcp_endpoint,
            httpx_client_factory=httpx_client_factory,
        )

    @staticmethod
    def _mcp_http_client_factory(
        *,
        api_key: str,
        proxy: str | None,
    ) -> Any:
        def factory(
            *,
            headers: dict[str, str] | None = None,
            **kwargs: Any,
        ) -> AsyncClient:
            return AsyncClient(
                **kwargs,
                proxy=proxy,
                headers={**(headers or {}), DOSU_API_KEY_HEADER: api_key},
            )

        return factory


__all__ = ["DstQuestionAgent"]
