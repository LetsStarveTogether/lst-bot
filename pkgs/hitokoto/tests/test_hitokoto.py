import os
import sqlite3
from asyncio import Future, gather, get_running_loop, timeout
from collections.abc import Mapping
from contextlib import closing
from json import dumps
from pathlib import Path
from time import time
from typing import Any, override

import hitokoto.client as client_module
import pytest
from hitokoto import (
    Hitokoto,
    HitokotoClient,
)
from hitokoto.cache import (
    is_cache_valid,
    read_cached_hitokoto,
    write_cache,
)
from pydantic import ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError

BUNDLE_URL = "https://sentences-bundle.hitokoto.cn/"


class RecordingPool(AsyncPoolManager):
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.calls: list[dict[str, object]] = []

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
        _ = body, fields, headers, json, urlopen_kw
        call: dict[str, object] = {"method": method, "url": url}
        self.calls.append(call)
        result = self.routes[url]
        if result is None:
            return await get_running_loop().create_future()
        if isinstance(result, AsyncHTTPResponse | RecordingResponse):
            return result
        payload = result if isinstance(result, bytes) else dumps(result).encode()
        return AsyncHTTPResponse(body=payload, status=200)


class RecordingResponse:
    def __init__(self, status: int, body: bytes | None = None) -> None:
        self.status = status
        self.body = body
        self.body_accessed = False

    @property
    def data(self) -> Future[bytes]:
        self.body_accessed = True
        future = get_running_loop().create_future()
        if self.body is not None:
            future.set_result(self.body)
        return future


def hitokoto_payload(text: str = "hello") -> dict[str, object]:
    return {
        "hitokoto": text,
        "from": "source",
        "from_who": "author",
        "id": 1,
    }


def bundle(text: str = "cached hello") -> list[Hitokoto]:
    return [Hitokoto.model_validate(hitokoto_payload(text))]


def bundle_routes(text: str = "cached hello") -> dict[str, object]:
    return {
        f"{BUNDLE_URL}version.json": {
            "protocol_version": "1.0.0",
            "sentences": [{"path": "./sentences/a.json"}],
        },
        f"{BUNDLE_URL}sentences/a.json": [
            item.model_dump(mode="json", by_alias=True) for item in bundle(text)
        ],
    }


def test_models_validate_only_used_official_fields() -> None:
    assert Hitokoto.model_validate(hitokoto_payload()).model_dump(by_alias=True) == {
        "hitokoto": "hello",
        "from": "source",
        "from_who": "author",
    }
    with pytest.raises(ValidationError):
        Hitokoto.model_validate(hitokoto_payload() | {"hitokoto": 1})
    with pytest.raises(ValidationError):
        client_module._BundleVersion.model_validate({  # ruff: ignore[private-member-access] - protocol boundary
            "protocol_version": "2.0.0",
            "sentences": [{"path": "./sentences/a.json"}],
        })


def test_hitokoto_format_preserves_text_and_partial_attributions() -> None:
    partial_source = Hitokoto.model_validate(
        hitokoto_payload() | {"from": "云雀叫了一整天》", "from_who": "木心"}
    )
    embedded_source = Hitokoto.model_validate(
        hitokoto_payload() | {"from": "帕斯卡，《思想录》", "from_who": None}
    )
    author_only = Hitokoto.model_validate(
        hitokoto_payload() | {"from": "", "from_who": "自创"}
    )
    anonymous = Hitokoto.model_validate(
        hitokoto_payload() | {"from": "", "from_who": None}
    )
    multiline = Hitokoto.model_validate(hitokoto_payload("甲\n乙"))

    assert str(partial_source).endswith("—— 木心《云雀叫了一整天》")
    assert str(embedded_source).endswith("—— 帕斯卡，《思想录》")
    assert str(author_only).endswith("—— 自创")
    assert "——" not in str(anonymous)
    assert "\u3000甲\n\u3000乙" in str(multiline)


async def test_client_consumes_error_body_before_failing(tmp_path: Path) -> None:
    response = RecordingResponse(500, b"error")
    pool = RecordingPool({f"{BUNDLE_URL}version.json": response})

    with pytest.raises(HTTPError, match="HTTP 500"):
        await HitokotoClient(
            http_pool=pool,
            cache_path=tmp_path / "hitokoto.db",
        ).get_hitokoto()

    assert response.body_accessed is True


@pytest.mark.parametrize(
    "route",
    [None, RecordingResponse(200)],
    ids=["request", "body"],
)
async def test_client_applies_wall_clock_timeout_to_all_io(
    monkeypatch: pytest.MonkeyPatch,
    route: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(client_module, "HTTP_TIMEOUT_SECONDS", 0.001)
    pool = RecordingPool({f"{BUNDLE_URL}version.json": route})

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await HitokotoClient(
                http_pool=pool,
                cache_path=tmp_path / "hitokoto.db",
            ).get_hitokoto()

    if isinstance(route, RecordingResponse):
        assert route.body_accessed is True


async def test_concurrent_reads_download_cache_once(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "hitokoto.db"
    pool = RecordingPool(bundle_routes())
    client = HitokotoClient(
        http_pool=pool,
        cache_path=cache_path,
    )

    results = await gather(*(client.get_hitokoto() for _ in range(8)))

    assert {result.hitokoto for result in results} == {"cached hello"}
    assert [call["url"] for call in pool.calls] == [
        f"{BUNDLE_URL}version.json",
        f"{BUNDLE_URL}sentences/a.json",
    ]
    assert await is_cache_valid(cache_path) is True


async def test_concurrent_atomic_cache_writes(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    values = {f"sentence {index}" for index in range(8)}

    await gather(*(write_cache(cache_path, bundle(value)) for value in values))

    result = await read_cached_hitokoto(cache_path)
    assert result.hitokoto in values
    assert await is_cache_valid(cache_path) is True
    assert not any(cache_path.parent.glob(f".{cache_path.name}.*.tmp"))


async def test_real_sqlite_cache_rejects_an_empty_database(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, ())

    with pytest.raises(RuntimeError, match="no matching"):
        await read_cached_hitokoto(cache_path)


async def test_cache_validity_handles_missing_and_current_database(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "hitokoto.db"

    assert await is_cache_valid(cache_path) is False
    await write_cache(cache_path, bundle())
    assert await is_cache_valid(cache_path) is True


@pytest.mark.parametrize(
    "offset_seconds",
    [
        -(73 * 60 * 60),
        60 * 60,
    ],
    ids=["stale", "future"],
)
async def test_cache_validity_rejects_invalid_mtime(
    tmp_path: Path,
    offset_seconds: int,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle())
    timestamp = time() + offset_seconds
    os.utime(cache_path, (timestamp, timestamp))

    assert await is_cache_valid(cache_path) is False


async def test_cache_validity_rejects_corruption(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    cache_path.write_bytes(b"not a sqlite database")

    assert await is_cache_valid(cache_path) is False
    client = HitokotoClient(
        http_pool=RecordingPool(bundle_routes("recovered")),
        cache_path=cache_path,
    )
    assert (await client.get_hitokoto()).hitokoto == "recovered"
    assert await is_cache_valid(cache_path) is True

    with closing(sqlite3.connect(cache_path)) as db, db:
        db.execute("UPDATE sentence SET payload = '{}'")

    assert await is_cache_valid(cache_path) is False


async def test_stale_cache_survives_refresh_failure(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle("stale"))
    timestamp = time() - 73 * 60 * 60
    os.utime(cache_path, (timestamp, timestamp))
    pool = RecordingPool({
        f"{BUNDLE_URL}version.json": RecordingResponse(503, b"error"),
    })

    result = await HitokotoClient(
        http_pool=pool,
        cache_path=cache_path,
    ).get_hitokoto()

    assert result.hitokoto == "stale"
    assert pool.calls == [{"method": "GET", "url": f"{BUNDLE_URL}version.json"}]


async def test_bundle_requires_a_sentence(tmp_path: Path) -> None:
    routes = bundle_routes()
    routes[f"{BUNDLE_URL}sentences/a.json"] = []
    client = HitokotoClient(
        http_pool=RecordingPool(routes),
        cache_path=tmp_path / "hitokoto.db",
    )

    with pytest.raises(ValidationError):
        await client.get_hitokoto()


async def test_bundle_allows_an_empty_part_when_another_has_sentences(
    tmp_path: Path,
) -> None:
    routes = bundle_routes()
    routes[f"{BUNDLE_URL}version.json"] = {
        "protocol_version": "1.0.0",
        "sentences": [
            {"path": "./sentences/a.json"},
            {"path": "./sentences/empty.json"},
        ],
    }
    routes[f"{BUNDLE_URL}sentences/empty.json"] = []
    pool = RecordingPool(routes)
    client = HitokotoClient(
        http_pool=pool,
        cache_path=tmp_path / "hitokoto.db",
    )

    await client.get_hitokoto()

    assert await is_cache_valid(client.cache_path)
    assert [call["url"] for call in pool.calls] == [
        f"{BUNDLE_URL}version.json",
        f"{BUNDLE_URL}sentences/a.json",
        f"{BUNDLE_URL}sentences/empty.json",
    ]
