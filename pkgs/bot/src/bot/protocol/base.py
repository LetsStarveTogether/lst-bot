from collections.abc import Mapping
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)

from bot.json import JSON_ADAPTER

_STRICT_BOOL = TypeAdapter(StrictBool).validate_python
_STRICT_INT = TypeAdapter(StrictInt).validate_python

type StrictBoolLiteral[T] = Annotated[T, BeforeValidator(_STRICT_BOOL)]
type StrictIntLiteral[T] = Annotated[T, BeforeValidator(_STRICT_INT)]


def _field_value(value: object, key: str, default: object = None) -> object:
    return (
        value.get(key, default)
        if isinstance(value, Mapping)
        else getattr(value, key, default)
    )


class Model(BaseModel):
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="allow",
        hide_input_in_errors=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def finite_json_numbers(cls, value: object, info: ValidationInfo) -> object:
        return JSON_ADAPTER.validate_python(value) if info.mode == "json" else value
