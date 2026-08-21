from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    model_validator,
)

_JSON_VALUE_ADAPTER = TypeAdapter(
    JsonValue,
    config=ConfigDict(allow_inf_nan=False),
)
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

    @model_validator(mode="after")
    def finite_json_numbers(self) -> Self:
        _JSON_VALUE_ADAPTER.validate_python(self.model_dump(mode="json"))
        return self
