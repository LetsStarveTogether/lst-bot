from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from logbook import DEBUG, INFO, TRACE
from pydantic import ValidationError

from lst_bot.settings import Settings


def test_log_level_accepts_names_and_numbers() -> None:
    named = Settings(
        _env_file=None,
        onebot_self_id="10000",
        log_level="info",
    )
    numeric = Settings(
        _env_file=None,
        onebot_self_id="10000",
        log_level="10",
    )
    default = Settings(
        _env_file=None,
        onebot_self_id="10000",
    )

    assert named.log_level == INFO
    assert numeric.log_level == DEBUG
    assert default.log_level == TRACE


def test_log_level_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            onebot_self_id="10000",
            log_level="verbose",
        )


def test_onebot_self_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEBOT_SELF_ID", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_multi_value_environment_variables_parse_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOT_CMD_PREFIXES", '["/", "!"]')
    monkeypatch.setenv("BOT_ADMIN", '["u1", "u2"]')
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("ONEBOT_SELF_ID", "10000")

    settings = Settings(_env_file=None)

    assert settings.bot_cmd_prefixes == ("/", "!")
    assert settings.bot_admin == {"u1", "u2"}
    assert settings.onebot_access_token.get_secret_value() == "secret"


def test_example_environment_loads_with_fifteen_minute_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    example = Path(__file__).parents[2] / ".env.example"

    settings = Settings(_env_file=example)

    assert settings.bot_timeout == timedelta(minutes=15)


def test_unknown_dotenv_field_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ONEBOT_SELF_ID=10000\nUNKNOWN_SETTING=value\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unknown_setting"):
        Settings(_env_file=env_file)
