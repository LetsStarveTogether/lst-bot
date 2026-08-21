from datetime import timedelta
from logging import DEBUG, getLevelNamesMapping
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, StrictInt, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = getLevelNamesMapping()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    bot_cmd_prefixes: tuple[str, ...] = ("/",)
    bot_admin: dict[str, frozenset[str]] = Field(default_factory=dict)
    bot_timeout: timedelta | None = Field(default=timedelta(seconds=900), gt=0)
    bot_timezone: ZoneInfo | None = None

    log_level: StrictInt = DEBUG
    http_proxy: str = "http://127.0.0.1:1080"

    onebot_self_id: str = ""
    onebot_ws_url: str = ""
    onebot_access_token: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    discord_bot_token: SecretStr = SecretStr("")
    discord_intents: int = Field(default=4609, ge=0)

    klei_access_token: SecretStr = SecretStr("")
    klei_host_id: str = ""

    openrouter_api_key: SecretStr = SecretStr("")
    dosu_mcp_endpoint: str = ""
    dosu_api_key: SecretStr = SecretStr("")

    report_group_id: str = ""

    @model_validator(mode="after")
    def validate_onebot_pair(self) -> Settings:
        if bool(self.onebot_ws_url) != bool(self.onebot_self_id):
            msg = "ONEBOT_WS_URL and ONEBOT_SELF_ID must be configured together"
            raise ValueError(msg)
        return self

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            level = _LOG_LEVELS.get(value.upper())
            value = int(value) if level is None else level
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value not in _LOG_LEVELS.values()
        ):
            msg = "log_level must be a standard Python logging level"
            raise ValueError(msg)
        return value
