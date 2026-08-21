from datetime import timedelta
from pathlib import Path

import pytest
from logbook import DEBUG, INFO, TRACE
from pydantic import ValidationError

from lst_bot.settings import Settings


def test_log_level_accepts_names_and_numbers() -> None:
    named = Settings(_env_file=None, log_level="info")
    numeric = Settings(_env_file=None, log_level="10")
    default = Settings(_env_file=None)

    assert named.log_level == INFO
    assert numeric.log_level == DEBUG
    assert default.log_level == TRACE


@pytest.mark.parametrize("value", ["verbose", True, 1.5, []])
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
    env_file.write_text(
        "UNKNOWN_SETTING=value\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unknown_setting"):
        Settings(_env_file=env_file)
