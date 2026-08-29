from datetime import timedelta
from logging import DEBUG, getLevelNamesMapping
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BeforeValidator,
    Field,
    Secret,
    SecretStr,
    StrictInt,
    UrlConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = getLevelNamesMapping()
type _OptionalSecretHttpUrl = Annotated[
    Secret[AnyHttpUrl] | None,
    BeforeValidator(lambda value: None if value == "" else value),
]


def _endpoint_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if any(
        part is not None
        for part in (value.username, value.password, value.query, value.fragment)
    ):
        msg = "URL credentials, query, and fragment are not allowed"
        raise ValueError(msg)
    return value


type _HttpsUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(allowed_schemes=["https"]),
    AfterValidator(_endpoint_url),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", hide_input_in_errors=True)

    bot_cmd_prefixes: tuple[str, ...] = ("/",)
    bot_admin: dict[str, frozenset[str]] = Field(default_factory=dict)
    bot_timeout: timedelta | None = Field(default=timedelta(seconds=900), gt=0)
    bot_timezone: ZoneInfo | None = None

    log_level: StrictInt = DEBUG
    http_proxy: _OptionalSecretHttpUrl = None

    onebot_self_id: str = ""
    onebot_ws_url: str = ""
    onebot_access_token: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    discord_bot_token: SecretStr = SecretStr("")
    discord_intents: int = Field(default=4609, ge=0)

    klei_access_token: SecretStr = SecretStr("")
    klei_host_id: str = ""

    openrouter_api_key: SecretStr = Field(min_length=1)
    dosu_mcp_endpoint: _HttpsUrl
    dosu_api_key: SecretStr = Field(min_length=1)

    report_group_id: str = ""

    @property
    def proxy_url(self) -> str | None:
        return str(self.http_proxy.get_secret_value()) if self.http_proxy else None

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
