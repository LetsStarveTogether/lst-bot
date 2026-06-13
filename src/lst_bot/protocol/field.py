from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer


def parse_unix_seconds(value: object) -> datetime:
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, bool):
        msg = "unix timestamp must not be a boolean"
        raise TypeError(msg)
    if isinstance(value, int | float):
        return _from_unix_seconds(float(value))
    if isinstance(value, str):
        return _parse_unix_seconds_string(value)
    msg = "unix timestamp must be a datetime, number, or numeric string"
    raise TypeError(msg)


def dump_unix_seconds_float(value: datetime) -> float:
    return _to_utc(value).timestamp()


def _parse_unix_seconds_string(value: str) -> datetime:
    value = value.strip()
    if not value:
        msg = "unix timestamp string must not be empty"
        raise ValueError(msg)
    try:
        return _from_unix_seconds(float(value))
    except ValueError:
        msg = "unix timestamp string must be numeric"
        raise ValueError(msg) from None


def _from_unix_seconds(value: float) -> datetime:
    if not isfinite(value):
        msg = "unix timestamp must be finite"
        raise ValueError(msg)
    return datetime.fromtimestamp(value, UTC)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


type UnixSecondsFloat = Annotated[
    datetime,
    BeforeValidator(parse_unix_seconds),
    PlainSerializer(dump_unix_seconds_float, return_type=float, when_used="json"),
]


__all__ = [
    "UnixSecondsFloat",
    "dump_unix_seconds_float",
    "parse_unix_seconds",
]
