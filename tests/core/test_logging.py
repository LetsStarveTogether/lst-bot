from __future__ import annotations

import logging
from pathlib import Path

import pytest
from logbook import DEBUG, INFO, TRACE
from logbook import TestHandler as LogbookTestHandler
from pydantic import ValidationError

from app.settings import Settings, logger


def test_settings_log_level_accepts_names_and_numbers() -> None:
    assert Settings.model_validate({"log_level": "info"}).log_level == INFO
    assert Settings.model_validate({"log_level": "10"}).log_level == DEBUG
    assert Settings(log_level=DEBUG).log_level == DEBUG


def test_settings_log_level_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"log_level": "verbose"})


def test_settings_log_level_defaults_like_starmerx_stream() -> None:
    assert Settings().log_level == TRACE


def test_settings_http_proxy_defaults_to_local_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    assert Settings().http_proxy == "http://127.0.0.1:1080"


def test_settings_multi_value_env_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_CMD_PREFIXES", '["/", "!"]')
    monkeypatch.setenv("BOT_ADMIN", '["u1", "u2"]')
    monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("REPORT_GROUP_ID", "10000")

    parsed = Settings()

    assert parsed.bot_cmd_prefixes == ("/", "!")
    assert parsed.bot_admin == {"u1", "u2"}
    assert parsed.onebot_access_token is not None
    assert parsed.onebot_access_token.get_secret_value() == "secret"
    assert parsed.report_group_id == "10000"


def test_settings_ignores_unrelated_dotenv_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'BOT_CMD_PREFIXES=["!"]\nREPORT_GROUP_ID=10000\nKLEI_HOST_ID=KU_test\n',
        encoding="utf-8",
    )

    parsed = Settings()

    assert parsed.bot_cmd_prefixes == ("!",)
    assert parsed.report_group_id == "10000"
    assert parsed.klei_host_id == "KU_test"


def test_module_logger_formats_keyword_arguments() -> None:
    with LogbookTestHandler() as handler:
        logger.info("hello {name}", name="world")

    assert handler.has_info("hello world", channel="app.settings")


def test_settings_redirects_stdlib_logging() -> None:
    with LogbookTestHandler() as handler:
        logging.getLogger("tests.legacy").warning("legacy %s", "warning")

    assert handler.has_warning("legacy warning", channel="tests.legacy")


def test_settings_suppresses_websockets_debug_logs() -> None:
    with LogbookTestHandler() as handler:
        logging.getLogger("websockets.client").debug("frame details")
        logging.getLogger("websockets.client").warning("connection warning")

    assert not handler.has_debug("frame details", channel="websockets.client")
    assert handler.has_warning("connection warning", channel="websockets.client")


def test_settings_suppresses_httpcore_http11_debug_logs() -> None:
    with LogbookTestHandler() as handler:
        logging.getLogger("httpcore.http11").debug("receive_response_headers")
        logging.getLogger("httpcore.http11").info("response complete")

    assert not handler.has_debug(
        "receive_response_headers",
        channel="httpcore.http11",
    )
    assert handler.has_info("response complete", channel="httpcore.http11")
