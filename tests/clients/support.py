from __future__ import annotations

from asyncio import sleep
from collections.abc import Awaitable, Iterable, Mapping
from typing import cast

import orjson
from pydantic import BaseModel, JsonValue


class FakeResponse:
    def __init__(self, payload: BaseModel | JsonValue | bytes) -> None:
        self.payload = payload

    @property
    def data(self) -> Awaitable[bytes]:
        if isinstance(self.payload, bytes):
            return sleep(0, result=self.payload)
        return sleep(0, result=orjson.dumps(_jsonable(self.payload)))


class FakePool:
    def __init__(self, payload: BaseModel | JsonValue | bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, JsonValue | Iterable[tuple[str, str]]]] = []
        self.cleared = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        fields: Iterable[tuple[str, str]] | None = None,
        json: JsonValue = None,
    ) -> FakeResponse:
        call: dict[str, JsonValue | Iterable[tuple[str, str]]] = {
            "method": method,
            "url": url,
            "json": json,
        }
        if fields is not None:
            call["fields"] = fields
        self.calls.append(call)
        return FakeResponse(self.payload)

    async def clear(self) -> None:
        self.cleared = True


class FakeRoutePool:
    def __init__(self, routes: Mapping[str, BaseModel | JsonValue | bytes]) -> None:
        self.routes = dict(routes)
        self.calls: list[dict[str, JsonValue | Iterable[tuple[str, str]]]] = []
        self.cleared = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        fields: Iterable[tuple[str, str]] | None = None,
        json: JsonValue = None,
    ) -> FakeResponse:
        call: dict[str, JsonValue | Iterable[tuple[str, str]]] = {
            "method": method,
            "url": url,
            "json": json,
        }
        if fields is not None:
            call["fields"] = fields
        self.calls.append(call)
        return FakeResponse(self.routes[url])

    async def clear(self) -> None:
        self.cleared = True


def fake_bundle_routes(hitokoto: str = "cached hello") -> dict[str, JsonValue]:
    base_url = "https://sentences-bundle.hitokoto.cn/"
    return {
        f"{base_url}version.json": {
            "protocol_version": "1.0.0",
            "bundle_version": "1.0.1",
            "updated_at": 1781163567796,
            "categories": {
                "path": "./categories.json",
                "timestamp": 1597712000881,
            },
            "sentences": [
                {
                    "name": "动画",
                    "key": "a",
                    "path": "./sentences/a.json",
                    "timestamp": 1619244060706,
                },
            ],
        },
        f"{base_url}categories.json": [
            {
                "id": 1,
                "name": "动画",
                "desc": "Anime - 动画",
                "key": "a",
                "created_at": "2020-05-15T10:48:09Z",
                "updated_at": "2020-05-15T10:48:12Z",
                "path": "./sentences/a.json",
            },
        ],
        f"{base_url}sentences/a.json": [
            {
                "id": 1,
                "uuid": "7bfb14e2-5538-4bde-8362-7e053f84e799",
                "hitokoto": hitokoto,
                "type": "a",
                "from": "cache source",
                "from_who": "cache author",
                "creator": "tester",
                "creator_uid": 1,
                "reviewer": 1,
                "commit_from": "web",
                "created_at": "1468605909",
                "length": len(hitokoto),
            },
        ],
    }


def _jsonable(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return cast(JsonValue, value)
