from collections.abc import Mapping
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    model_validator,
)

_JSON_VALUE_ADAPTER = TypeAdapter(
    JsonValue,
    config=ConfigDict(allow_inf_nan=False),
)


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
