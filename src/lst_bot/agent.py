from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from json import dumps
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import MCPError
from httpx2 import AsyncClient, Timeout
from jsonschema import ValidationError
from jsonschema.validators import validator_for
from pydantic_ai import Agent, ModelRetry, Tool, WebSearchTool
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from .settings import Settings

REQUEST_TIMEOUT = 600

DST_AGENT_INSTRUCTIONS = """\
你是《饥荒联机版》（Don't Starve Together）的问答助手，名字叫拾什。
目前你作为机器人在聊天平台的群聊中回答玩家关于 DST 的问题。
你服务的是一个玩家自发组织的开放 DST 社区，
社区的正式名称是 Let's Starve Together。
“朗诵团”是其缩写 LST 的中文译名。

你可以使用这些工具：
- web_search：查询公开网页信息，成本更低且速度更快，适合优先使用。
  用它补充 Klei 公告、版本更新、近期改动、社区资料，也用它寻找
  DST Lua 代码实体标识符，例如 prefab、component、stategraph、action、
  recipe、tuning、event、function、constant 或文件路径。
- read_knowledge：跨已索引的数据源检索，并综合多个来源给出带引用的答案。
  背后的主要资料是 DST 游戏 Lua 脚本代码。为了获得更好的结果，
  提问应尽量包含具体 Lua 代码实体标识符，并把问题写成清晰的代码语境。

工具使用策略：
- 简单稳定的问题可以直接回答；其他问题通常先用 web_search 获取公开线索。
- 复杂机制、代码实现、模组开发或服务器配置问题，在调用 read_knowledge 前，先用
  web_search 和推理明确问题描述，尽量找出相关 Lua 实体标识符。
- 当问题已经有清晰代码实体，或需要从游戏 Lua 脚本代码中综合确认时，调用 read_knowledge。
- 工具结果不足或互相冲突时说明不确定，并区分 Lua 代码索引结论和公开资料结论。

解释方式：
- 最终回答参照费曼学习法：先用一句话给结论，再用玩家熟悉的游戏现象解释原因。
- 默认读者不了解 Lua、prefab、component、stategraph 等代码概念；必须先讲白话，
  再在确有必要时补充代码名或服务器配置名。
- 避免堆叠代码细节；只保留能帮助判断、操作或避免误解的关键依据。

回答要求：
- 少于 500 字的中文（在不影响语义的前提下尽可能简短）。
- 不使用 markdown 标记，只用基本的空格和换行排版。
- 语气友好俏皮，带一点幽默调侃，不要客套和招呼。
- 不编造版本机制、角色数值、代码或服务器配置。
- 不需要引用或列出原始网页链接。
"""


@asynccontextmanager
async def build_question_agent(
    settings: Settings,
    *,
    http_client: AsyncClient,
) -> AsyncIterator[Agent]:
    def create_dosu_client(*_args: object, **_kwargs: object) -> AsyncClient:
        # FastMCP owns each session's client; retain our HTTP policy over its defaults.
        return AsyncClient(
            headers={"X-Dosu-API-Key": settings.dosu_api_key.get_secret_value()},
            proxy=settings.proxy_url,
            timeout=Timeout(REQUEST_TIMEOUT, connect=5),
            http2=True,
            trust_env=False,
            follow_redirects=False,
        )

    async with Client(
        StreamableHttpTransport(
            settings.dosu_mcp_endpoint,
            httpx_client_factory=create_dosu_client,
        ),
        timeout=REQUEST_TIMEOUT,
        init_timeout=REQUEST_TIMEOUT,
    ) as knowledge:
        definition = next(
            (
                tool
                for tool in await knowledge.list_tools()
                if tool.name == "read_knowledge"
            ),
            None,
        )
        if definition is None:
            msg = "Dosu MCP does not provide read_knowledge"
            raise RuntimeError(msg)
        validator_type = validator_for(definition.input_schema)
        validator_type.check_schema(definition.input_schema)
        validator = validator_type(definition.input_schema)

        async def read_knowledge(**arguments: Any) -> Any:
            try:
                validator.validate(arguments)
                result = await knowledge.call_tool(
                    "read_knowledge", arguments, raise_on_error=False
                )
            except ValidationError as error:
                raise ModelRetry(error.message) from error
            except MCPError as error:
                raise ModelRetry(str(error)) from error
            content = [
                content.text
                if content.type == "text"
                else content.model_dump(mode="json", by_alias=True)
                for content in result.content
            ]
            output = (
                result.structured_content
                if result.structured_content is not None
                else content[0]
                if len(content) == 1
                else content
            )
            if result.is_error:
                message = (
                    output
                    if isinstance(output, str)
                    else dumps(output, ensure_ascii=False)
                )
                raise ModelRetry(message if output else "Dosu read_knowledge failed")
            return output

        async with Agent(
            OpenRouterModel(
                "deepseek/deepseek-v4-pro-0813",
                provider=OpenRouterProvider(
                    api_key=settings.openrouter_api_key.get_secret_value(),
                    http_client=http_client,
                ),
            ),
            instructions=DST_AGENT_INSTRUCTIONS,
            tools=[
                Tool.from_schema(
                    read_knowledge,
                    name=definition.name,
                    description=definition.description,
                    json_schema=definition.input_schema,
                )
            ],
            capabilities=[NativeTool(WebSearchTool())],
        ) as agent:
            yield agent
