from datetime import timedelta
from logging import DEBUG, INFO
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lst_bot.settings import Settings


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OPENROUTER_API_KEY", None),
        ("DOSU_MCP_ENDPOINT", None),
        ("DOSU_API_KEY", None),
        ("OPENROUTER_API_KEY", ""),
        ("DOSU_MCP_ENDPOINT", "invalid"),
        ("DOSU_MCP_ENDPOINT", "http://example.com/mcp"),
        ("DOSU_API_KEY", ""),
    ],
)
def test_ai_service_settings_are_required_and_validated(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_http_proxy_defaults_to_direct_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "")
    settings = Settings(_env_file=None)
    assert settings.http_proxy is None
    assert settings.proxy_url is None


def test_http_proxy_credentials_stay_secret() -> None:
    settings = Settings(
        _env_file=None,
        http_proxy="http://alice:password@example.com",
    )

    assert settings.http_proxy is not None
    assert settings.proxy_url == "http://alice:password@example.com/"
    for output in (
        repr(settings),
        repr(settings.model_dump()),
        repr(settings.model_dump(mode="json")),
        settings.model_dump_json(),
    ):
        assert "alice" not in output
        assert "password" not in output


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user@example.com/mcp",
        "https://:password@example.com/mcp",
        "https://example.com/mcp?query=value",
        "https://example.com/mcp#fragment",
    ],
    ids=("username", "password", "query", "fragment"),
)
def test_dosu_endpoint_rejects_non_route_url_parts(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="not allowed"):
        Settings(_env_file=None, dosu_mcp_endpoint=endpoint)


@pytest.mark.parametrize(
    ("field", "value", "password"),
    [
        ("http_proxy", "http://alice:proxy-secret@", "proxy-secret"),
        (
            "dosu_mcp_endpoint",
            "https://alice:endpoint-secret@example.com/mcp",
            "endpoint-secret",
        ),
    ],
    ids=("proxy", "endpoint"),
)
def test_invalid_url_errors_hide_credentials(
    field: str,
    value: str,
    password: str,
) -> None:
    overrides: dict[str, Any] = {field: value}
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, **overrides)

    error = str(exc_info.value)
    assert "alice" not in error
    assert password not in error


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
    monkeypatch.setenv("DISCORD_INTENTS", "37377")
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")

    settings = Settings(_env_file=None)

    assert settings.bot_cmd_prefixes == ("/", "!")
    assert settings.bot_admin == {"qq": {"u1"}, "discord": {"u2"}}
    assert settings.discord_intents == 37377
    assert settings.onebot_access_token.get_secret_value() == "secret"


@pytest.mark.parametrize(
    "value",
    [True, 4609.0, "4609.0"],
    ids=("boolean", "float", "float-string"),
)
def test_discord_intents_rejects_lossy_coercion(value: Any) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, discord_intents=value)


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
