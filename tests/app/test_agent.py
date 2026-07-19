from pydantic import SecretStr
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.profiles.grok import grok_model_profile

from lst_bot.agent import DST_AGENT_INSTRUCTIONS, GROK_MODEL, DstQuestionAgent


def test_grok_model_supports_question_agent_capabilities() -> None:
    profile = grok_model_profile(GROK_MODEL)

    assert profile is not None
    assert profile["supports_thinking"]
    assert profile["thinking_always_enabled"]
    assert WebSearchTool in profile["supported_native_tools"]
    assert "web_search" in DST_AGENT_INSTRUCTIONS
    assert "google_search" not in DST_AGENT_INSTRUCTIONS


def test_question_agent_keeps_both_model_keys() -> None:
    gemini_api_key = SecretStr("gemini")
    xai_api_key = SecretStr("xai")
    agent = DstQuestionAgent(
        gemini_api_key=gemini_api_key,
        xai_api_key=xai_api_key,
        dosu_mcp_endpoint="https://example.com/mcp",
        dosu_api_key=SecretStr("dosu"),
    )

    assert agent._gemini_api_key is gemini_api_key
    assert agent._xai_api_key is xai_api_key
