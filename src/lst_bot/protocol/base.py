from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Model(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        serialize_by_alias=True,
    )


__all__ = ["Model"]
