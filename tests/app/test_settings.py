from datetime import timedelta
from logging import DEBUG, INFO
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lst_bot.settings import Settings


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "openrouter_api_key": "",
            "dosu_mcp_endpoint": "https://example.com/mcp",
            "dosu_api_key": "test",
        },
        {
            "openrouter_api_key": "test",
            "dosu_mcp_endpoint": "invalid",
            "dosu_api_key": "test",
        },
        {
            "openrouter_api_key": "test",
            "dosu_mcp_endpoint": "http://example.com/mcp",
            "dosu_api_key": "test",
        },
        {
            "openrouter_api_key": "test",
            "dosu_mcp_endpoint": "https://example.com/mcp",
            "dosu_api_key": "",
        },
    ],
)
def test_ai_service_settings_are_required_and_validated(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delenv("DOSU_MCP_ENDPOINT")
    monkeypatch.delenv("DOSU_API_KEY")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_empty_http_proxy_means_direct_connection() -> None:
    assert Settings(_env_file=None, http_proxy="").http_proxy is None


def test_log_level_accepts_names_and_numbers() -> None:
    named = Settings(_env_file=None, log_level="info")
    numeric = Settings(_env_file=None, log_level="10")
    default = Settings(_env_file=None)

    assert named.log_level == INFO
    assert numeric.log_level == DEBUG
    assert default.log_level == DEBUG


@pytest.mark.parametrize("value", ["verbose", "TRACE", 9, True, 1.5, []])
def test_log_level_rejects_invalid_value(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level=value)


@pytest.mark.parametrize(
    ("onebot_ws_url", "onebot_self_id"),
    [("ws://127.0.0.1:6700", ""), ("", "10000")],
)
def test_onebot_url_and_identity_are_configured_together(
    onebot_ws_url: str,
    onebot_self_id: str,
) -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(
            _env_file=None,
            onebot_ws_url=onebot_ws_url,
            onebot_self_id=onebot_self_id,
        )


def test_multi_value_environment_variables_parse_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_CMD_PREFIXES", '["/", "!"]')
    monkeypatch.setenv("BOT_ADMIN", '{"qq":["u1"],"discord":["u2"]}')
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")

    settings = Settings(_env_file=None)

    assert settings.bot_cmd_prefixes == ("/", "!")
    assert settings.bot_admin == {"qq": {"u1"}, "discord": {"u2"}}
    assert settings.onebot_access_token.get_secret_value() == "secret"


def test_bot_timeout_configuration() -> None:
    example = Path(__file__).parents[2] / ".env.example"

    settings = Settings(_env_file=example)

    assert settings.bot_timeout == timedelta(minutes=15)
    assert Settings(_env_file=None, bot_timeout=None).bot_timeout is None
    for timeout in (0, -1):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, bot_timeout=timeout)


def test_unknown_dotenv_field_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("UNKNOWN_SETTING=value\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="unknown_setting"):
        Settings(_env_file=env_file)
