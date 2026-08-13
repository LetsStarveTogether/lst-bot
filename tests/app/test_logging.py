from __future__ import annotations

import logging

import pytest
from logbook import INFO
from logbook import TestHandler as LogbookTestHandler

from lst_bot.settings import Settings, configure_logging


def exercise_logging_context(
    settings: Settings,
    library_loggers: tuple[logging.Logger, ...],
) -> None:
    with configure_logging(settings), LogbookTestHandler() as handler:
        logging.getLogger("tests.legacy").warning("legacy warning")
        assert handler.has_warning("legacy warning", channel="tests.legacy")
        assert all(logger.level == logging.INFO for logger in library_loggers)
        msg = "stop"
        raise RuntimeError(msg)


def test_configure_logging_redirects_and_restores_global_state() -> None:
    root = logging.getLogger()
    handlers = root.handlers[:]
    root_level = root.level
    library_loggers = tuple(
        logging.getLogger(name) for name in ("httpcore", "websockets", "mcp")
    )
    library_levels = tuple(logger.level for logger in library_loggers)
    settings = Settings(
        _env_file=None,
        onebot_self_id="10000",
        log_level=INFO,
    )

    with pytest.raises(RuntimeError, match="stop"):
        exercise_logging_context(settings, library_loggers)

    assert root.handlers == handlers
    assert root.level == root_level
    assert tuple(logger.level for logger in library_loggers) == library_levels
