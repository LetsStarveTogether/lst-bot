from json import dumps
from typing import Any

from pydantic import ConfigDict, JsonValue, TypeAdapter
from pydantic_core import from_json as _loads

JSON_ADAPTER = TypeAdapter(JsonValue, config=ConfigDict(allow_inf_nan=False))


def dumpb(value: object) -> bytes:
    text = dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    return text.encode()


def loads(value: str | bytes) -> Any:
    return JSON_ADAPTER.validate_python(_loads(value, allow_inf_nan=False))
