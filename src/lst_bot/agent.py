from functools import partial
from typing import Any

from fastmcp.client.transports import StreamableHttpTransport
from httpx import AsyncClient
from pydantic_ai import Agent, WebSearchTool
from pydantic_ai.capabilities import NativeTool
from pydantic_ai.mcp import MCPToolset
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
- ask：跨已索引的数据源提问，并综合多个来源给出带引用的答案。
  背后的主要资料是 DST 游戏 Lua 脚本代码。为了获得更好的结果，
  提问应尽量包含具体 Lua 代码实体标识符，并把问题写成清晰的代码语境。
  省略 data_source_ids 参数。

工具使用策略：
- 简单稳定的问题可以直接回答；其他问题通常先用 web_search 获取公开线索。
- 复杂机制、代码实现、模组开发或服务器配置问题，在调用 ask 前，先用
  web_search 和推理明确问题描述，尽量找出相关 Lua 实体标识符。
- 当问题已经有清晰代码实体，或需要从游戏 Lua 脚本代码中综合确认时，调用 ask。
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


def mcp_http_client(configured_proxy: str | None, **kwargs: Any) -> AsyncClient:
    kwargs.update(proxy=configured_proxy, trust_env=False, follow_redirects=False)
    return AsyncClient(**kwargs)


def build_question_agent(
    settings: Settings,
    *,
    http_client: AsyncClient,
) -> Agent:
    proxy = str(settings.http_proxy) if settings.http_proxy else None
    return Agent(
        OpenRouterModel(
            "deepseek/deepseek-v4-pro-0813",
            provider=OpenRouterProvider(
                api_key=settings.openrouter_api_key.get_secret_value(),
                http_client=http_client,
            ),
        ),
        instructions=DST_AGENT_INSTRUCTIONS,
        toolsets=[
            MCPToolset(
                StreamableHttpTransport(
                    settings.dosu_mcp_endpoint,
                    headers={
                        "X-Dosu-API-Key": settings.dosu_api_key.get_secret_value()
                    },
                    httpx_client_factory=partial(mcp_http_client, proxy),  # ty: ignore[invalid-argument-type]
                ),
                init_timeout=REQUEST_TIMEOUT,
                read_timeout=REQUEST_TIMEOUT,
            ).filtered(lambda _, tool_def: tool_def.name == "ask")
        ],
        capabilities=[NativeTool(WebSearchTool())],
    )
