from __future__ import annotations

import json as jsonlib
from collections.abc import Mapping
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

import apsw
import pytest
from hitokoto import (
    HitokotoBundle,
    HitokotoClient,
    HitokotoType,
    bundle_base_url,
    bundle_file_url,
    is_cache_valid,
    read_cached_hitokoto,
    write_cache,
)
from pydantic import JsonValue
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError

API_URL = "https://hitokoto.example.test/"
BUNDLE_URL = "https://bundle.example.test/"


class RecordingPool(AsyncPoolManager):
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.calls: list[dict[str, object]] = []
        self.cleared = False

    @override
    async def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        fields: Any = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        **urlopen_kw: Any,
    ) -> Any:
        _ = body, headers, urlopen_kw
        call: dict[str, object] = {"method": method, "url": url}
        if fields is not None:
            call["fields"] = list(fields)
        if json is not None:
            call["json"] = json
        self.calls.append(call)
        result = self.routes[url]
        if isinstance(result, Exception):
            raise result
        body = result if isinstance(result, bytes) else jsonlib.dumps(result).encode()
        return AsyncHTTPResponse(body=body)

    @override
    async def clear(self) -> None:
        self.cleared = True


def hitokoto_payload(text: str = "hello") -> dict[str, JsonValue]:
    return {
        "id": 1,
        "uuid": "7bfb14e2-5538-4bde-8362-7e053f84e799",
        "hitokoto": text,
        "type": "a",
        "from": "source",
        "from_who": "author",
        "creator": "tester",
        "creator_uid": 1,
        "reviewer": 1,
        "commit_from": "web",
        "created_at": "2026-06-12T00:00:00Z",
    }


def bundle(text: str = "cached hello", *, include_game: bool = False) -> HitokotoBundle:
    categories: list[dict[str, JsonValue]] = [
        {
            "id": 1,
            "name": "动画",
            "desc": "Anime",
            "key": "a",
            "created_at": "2020-05-15T10:48:09Z",
            "updated_at": "2020-05-15T10:48:12Z",
            "path": "./sentences/a.json",
        },
    ]
    sentences: list[dict[str, JsonValue]] = [
        {
            **hitokoto_payload(text),
            "created_at": "1468605909",
            "length": len(text),
        },
    ]
    if include_game:
        categories.append({
            "id": 3,
            "name": "游戏",
            "desc": "Game",
            "key": "c",
            "created_at": "2020-05-15T10:48:09Z",
            "updated_at": "2020-05-15T10:48:12Z",
            "path": "./sentences/c.json",
        })
        sentences.append({
            **hitokoto_payload("cached game"),
            "id": 3,
            "uuid": "0ed43f7f-7af4-4f06-8665-101855d66d74",
            "type": "c",
            "from_who": None,
            "created_at": "1468605909",
            "length": 11,
        })
    return HitokotoBundle.model_validate({
        "protocol_version": "1.0.0",
        "bundle_version": "1.0.1",
        "categories": categories,
        "sentences": sentences,
    })


def bundle_routes(text: str = "cached hello") -> dict[str, object]:
    value = bundle(text)
    return {
        f"{BUNDLE_URL}version.json": {
            "protocol_version": value.protocol_version,
            "bundle_version": value.bundle_version,
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
        f"{BUNDLE_URL}categories.json": [
            item.model_dump(mode="json", by_alias=True) for item in value.categories
        ],
        f"{BUNDLE_URL}sentences/a.json": [
            item.model_dump(mode="json", by_alias=True) for item in value.sentences
        ],
    }


@pytest.mark.parametrize(
    ("types", "expected_fields"),
    [
        (None, None),
        ((HitokotoType.ANIME, HitokotoType.GAME), [("c", "a"), ("c", "c")]),
    ],
    ids=["unfiltered", "multiple-types"],
)
async def test_client_requests_api_with_optional_type_filters(
    types: tuple[HitokotoType, ...] | None,
    expected_fields: list[tuple[str, str]] | None,
) -> None:
    pool = RecordingPool({API_URL: hitokoto_payload()})
    client = HitokotoClient(
        url=API_URL,
        http_pool=pool,
    )

    async with client:
        result = await client.get_hitokoto(types)

    expected_call: dict[str, object] = {"method": "GET", "url": API_URL}
    if expected_fields is not None:
        expected_call["fields"] = expected_fields
    assert result.hitokoto == "hello"
    assert pool.calls == [expected_call]
    assert pool.cleared is True


async def test_client_closes_pool_when_request_fails() -> None:
    pool = RecordingPool({API_URL: HTTPError("offline")})
    client = HitokotoClient(
        url=API_URL,
        http_pool=pool,
    )

    with pytest.raises(HTTPError, match="offline"):
        async with client:
            await client.get_hitokoto()

    assert pool.cleared is True


async def test_client_populates_and_reuses_real_sqlite_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "hitokoto.db"
    pool = RecordingPool(bundle_routes())
    client = HitokotoClient(
        bundle_url=BUNDLE_URL,
        http_pool=pool,
        cache_path=cache_path,
    )

    async with client:
        first = await client.get_hitokoto((HitokotoType.ANIME,), use_cache=True)
        download_calls = list(pool.calls)
        second = await client.get_hitokoto((HitokotoType.ANIME,), use_cache=True)

    assert first.hitokoto == second.hitokoto == "cached hello"
    assert cache_path.is_file()
    assert await is_cache_valid(cache_path) is True
    assert pool.calls == download_calls
    assert [call["url"] for call in pool.calls] == [
        f"{BUNDLE_URL}version.json",
        f"{BUNDLE_URL}categories.json",
        f"{BUNDLE_URL}sentences/a.json",
    ]
    assert pool.cleared is True


async def test_real_sqlite_cache_filters_types_and_rejects_empty_matches(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle(include_game=True))

    result = await read_cached_hitokoto(cache_path, (HitokotoType.GAME,))

    assert result.hitokoto == "cached game"
    with pytest.raises(RuntimeError, match="no matching"):
        await read_cached_hitokoto(cache_path, (HitokotoType.JOKE,))


async def test_cache_validity_handles_missing_and_current_database(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "hitokoto.db"

    assert await is_cache_valid(cache_path) is False
    await write_cache(cache_path, bundle())
    assert await is_cache_valid(cache_path) is True


@pytest.mark.parametrize(
    "updated_at",
    [
        (datetime.now(UTC) - timedelta(hours=73)).isoformat(),
        "not-a-date",
    ],
    ids=["stale", "malformed-timestamp"],
)
async def test_cache_validity_rejects_bad_update_time(
    tmp_path: Path,
    updated_at: str,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle())
    async with aclosing(await apsw.Connection.as_async(str(cache_path))) as db:
        await db.execute("UPDATE version SET updated_at = ?", (updated_at,))

    assert await is_cache_valid(cache_path) is False


@pytest.mark.parametrize(
    ("url", "path", "expected"),
    [
        (
            "https://bundle.example.test/api?unused=1#fragment",
            "./sentences/a.json",
            "https://bundle.example.test/api/sentences/a.json",
        ),
        (
            "bundle.example.test",
            "/version.json",
            "https://bundle.example.test/version.json",
        ),
    ],
    ids=["strip-query-and-fragment", "supply-scheme-and-root-path"],
)
def test_bundle_url_helpers_normalize_paths(
    url: str,
    path: str,
    expected: str,
) -> None:
    assert bundle_file_url(bundle_base_url(url), path) == expected


def test_bundle_base_url_rejects_empty_value() -> None:
    with pytest.raises(RuntimeError, match="empty"):
        bundle_base_url(" ")
