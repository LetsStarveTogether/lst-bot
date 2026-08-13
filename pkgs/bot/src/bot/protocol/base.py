from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Model(BaseModel):
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="allow",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    @model_validator(mode="before")
    @classmethod
    def finite_json_numbers(cls, value: object) -> object:
        pending = [value]
        visited: set[int] = set()
        while pending:
            item = pending.pop()
            if isinstance(item, float) and not isfinite(item):
                msg = "JSON numbers must be finite"
                raise ValueError(msg)
            if isinstance(item, BaseModel):
                item = item.model_dump()
            if isinstance(item, Mapping):
                values = item.values()
            elif isinstance(item, list | tuple):
                values = item
            else:
                continue
            if id(item) in visited:
                continue
            visited.add(id(item))
            pending.extend(values)
        return value


__all__ = ["Model"]
