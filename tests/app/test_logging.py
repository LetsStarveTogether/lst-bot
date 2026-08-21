from __future__ import annotations

import logging

import pytest
from logbook import INFO
from logbook import TestHandler as LogbookTestHandler

from lst_bot.settings import Settings, configure_logging


def test_configure_logging_redirects_and_restores_global_state() -> None:
    root = logging.getLogger()
    handlers = root.handlers[:]
    root_level = root.level
    library_loggers = tuple(
        logging.getLogger(name)
        for name in ("httpcore", "urllib3_future", "websockets", "mcp")
    )
    library_levels = tuple(logger.level for logger in library_loggers)
    settings = Settings(_env_file=None, log_level=INFO)

    with (  # ruff: ignore[pytest-raises-with-multiple-statements]
        pytest.raises(RuntimeError, match="stop"),
        configure_logging(settings),
        LogbookTestHandler() as handler,
    ):
        logging.getLogger("tests.legacy").warning("legacy warning")
        logging.getLogger("urllib3_future.connectionpool").debug(
            "request https://api.telegram.org/botSECRET/getMe"
        )
        assert handler.has_warning("legacy warning", channel="tests.legacy")
        assert all("SECRET" not in record.message for record in handler.records)
        assert all(logger.level == logging.INFO for logger in library_loggers)
        msg = "stop"
        raise RuntimeError(msg)

    assert root.handlers == handlers
    assert root.level == root_level
    assert tuple(logger.level for logger in library_loggers) == library_levels
