from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo

from logbook import DEBUG, NOTSET, TRACE, lookup_level
from logbook.compat import redirect_logging
from logbook.more import ColorizedStderrHandler
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    bot_cmd_prefixes: tuple[str, ...] = ("/",)
    bot_admin: frozenset[str] = Field(default_factory=frozenset)
    bot_timeout: timedelta | None = timedelta(seconds=900)
    bot_timezone: ZoneInfo | None = None

    log_level: int = NOTSET
    http_proxy: str = "http://127.0.0.1:1080"

    onebot_self_id: str
    onebot_ws_url: str = ""
    onebot_access_token: SecretStr = SecretStr("")

    klei_access_token: SecretStr = SecretStr("")
    klei_host_id: str = ""

    openrouter_api_key: SecretStr = SecretStr("")
    dosu_mcp_endpoint: str = ""
    dosu_api_key: SecretStr = SecretStr("")

    report_group_id: str = ""

    @field_validator("log_level", mode="plain")
    @classmethod
    def validate_log_level(cls, value: int | str) -> int:
        try:
            level = lookup_level(value.upper() if isinstance(value, str) else value)
        except LookupError:
            level = lookup_level(int(value))

        if level == NOTSET:
            level = TRACE if __debug__ else DEBUG

        return level


@contextmanager
def configure_logging(settings: Settings) -> Iterator[None]:
    root = logging.getLogger()
    handlers = root.handlers[:]
    root_level = root.level
    library_loggers = tuple(
        logging.getLogger(name) for name in ("httpcore", "websockets", "mcp")
    )
    library_levels = tuple(logger.level for logger in library_loggers)
    handler = ColorizedStderrHandler(level=settings.log_level)

    redirect_logging()
    for logger in library_loggers:
        logger.setLevel(logging.INFO)
    handler.push_application()
    try:
        yield
    finally:
        handler.pop_application()
        root.handlers[:] = handlers
        root.setLevel(root_level)
        for logger, level in zip(library_loggers, library_levels, strict=True):
            logger.setLevel(level)


__all__ = ["Settings", "configure_logging"]
