from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from logbook import DEBUG, NOTSET, TRACE, Logger, lookup_level
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

    onebot_ws_url: str = ""
    onebot_access_token: SecretStr = SecretStr("")

    klei_access_token: SecretStr = SecretStr("")
    klei_host_id: str = ""

    gemini_api_key: SecretStr = SecretStr("")
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


settings = Settings()
logger = Logger(__name__)
redirect_logging()
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("websockets").setLevel(logging.INFO)
logging.getLogger("mcp").setLevel(logging.INFO)
ColorizedStderrHandler(level=settings.log_level).push_application()
logger.notice(
    "settings loaded: log={log_level} admins={admin_count} prefixes={prefixes}",
    log_level=settings.log_level,
    admin_count=len(settings.bot_admin),
    prefixes=",".join(settings.bot_cmd_prefixes),
)

__all__ = ["Settings", "logger"]
