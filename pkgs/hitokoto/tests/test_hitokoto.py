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
    HitokotoType,
)
from hitokoto.cache import (
    is_cache_valid,
    read_cached_hitokoto,
    write_cache,
)
from pydantic import JsonValue, ValidationError
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
        _ = body, headers, json, urlopen_kw
        call: dict[str, object] = {"method": method, "url": url}
        if fields is not None:
            call["fields"] = list(fields)
        self.calls.append(call)
        result = self.routes[url]
        if result is None:
            return await get_running_loop().create_future()
        if isinstance(result, Exception):
            raise result
        if isinstance(result, AsyncHTTPResponse | PendingBodyResponse):
            return result
        payload = result if isinstance(result, bytes) else dumps(result).encode()
        return AsyncHTTPResponse(body=payload, status=200)

    @override
    async def clear(self) -> None:
        self.cleared = True


class PendingBodyResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.body_accessed = False

    @property
    def data(self) -> Future[bytes]:
        self.body_accessed = True
        return get_running_loop().create_future()


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


def bundle(text: str = "cached hello", *, include_game: bool = False) -> list[Hitokoto]:
    sentences: list[dict[str, JsonValue]] = [
        {
            **hitokoto_payload(text),
            "created_at": "1468605909",
        },
    ]
    if include_game:
        sentences.append({
            **hitokoto_payload("cached game"),
            "id": 3,
            "uuid": "0ed43f7f-7af4-4f06-8665-101855d66d74",
            "type": "c",
            "from_who": None,
            "created_at": "1468605909",
        })
    return [Hitokoto.model_validate(item) for item in sentences]


def bundle_routes(text: str = "cached hello") -> dict[str, object]:
    return {
        f"{BUNDLE_URL}version.json": {
            "sentences": [{"path": "./sentences/a.json"}],
        },
        f"{BUNDLE_URL}sentences/a.json": [
            item.model_dump(mode="json", by_alias=True) for item in bundle(text)
        ],
    }


def test_model_strictly_validates_identifiers_and_parses_official_time() -> None:
    payload = hitokoto_payload() | {"created_at": "1468605909"}
    assert Hitokoto.model_validate(payload).created_at.timestamp() == 1468605909

    for field in ("id", "creator_uid", "reviewer"):
        with pytest.raises(ValidationError):
            Hitokoto.model_validate(payload | {field: True})
    with pytest.raises(ValidationError):
        Hitokoto.model_validate(payload | {"id": -1})


@pytest.mark.parametrize(
    ("types", "expected_fields"),
    [
        (None, None),
        ((HitokotoType.ANIME, HitokotoType.GAME), [("c", "a"), ("c", "c")]),
    ],
)
async def test_client_requests_api_with_optional_type_filters(
    types: tuple[HitokotoType, ...] | None,
    expected_fields: list[tuple[str, str]] | None,
) -> None:
    pool = RecordingPool({API_URL: hitokoto_payload()})
    client = HitokotoClient(url=API_URL, http_pool=pool)

    async with client:
        result = await client.get_hitokoto(types)

    expected_call: dict[str, object] = {"method": "GET", "url": API_URL}
    if expected_fields is not None:
        expected_call["fields"] = expected_fields
    assert result.hitokoto == "hello"
    assert pool.calls == [expected_call]
    assert pool.cleared is False


async def test_client_strictly_validates_type_filters() -> None:
    client = HitokotoClient(http_pool=RecordingPool({}))

    with pytest.raises(ValidationError):
        await client.get_hitokoto(("a",))  # ty: ignore[invalid-argument-type]


async def test_client_closes_owned_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = RecordingPool({API_URL: hitokoto_payload()})
    monkeypatch.setattr(client_module, "AsyncPoolManager", lambda: pool)

    async with HitokotoClient(url=API_URL) as client:
        await client.get_hitokoto()

    assert pool.cleared is True


async def test_client_rejects_error_status_without_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "HTTP_TIMEOUT_SECONDS", 0.01)
    response = PendingBodyResponse(500)
    pool = RecordingPool({API_URL: response})

    with pytest.raises(HTTPError, match="HTTP 500"):
        await HitokotoClient(url=API_URL, http_pool=pool).get_hitokoto()

    assert response.body_accessed is False


@pytest.mark.parametrize(
    "route",
    [None, PendingBodyResponse(200)],
    ids=["request", "body"],
)
async def test_client_applies_wall_clock_timeout_to_all_io(
    monkeypatch: pytest.MonkeyPatch,
    route: object,
) -> None:
    monkeypatch.setattr(client_module, "HTTP_TIMEOUT_SECONDS", 0.001)
    pool = RecordingPool({API_URL: route})

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await HitokotoClient(url=API_URL, http_pool=pool).get_hitokoto()

    if isinstance(route, PendingBodyResponse):
        assert route.body_accessed is True
    assert pool.cleared is False


async def test_concurrent_clients_download_cache_once(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "hitokoto.db"
    pool = RecordingPool(bundle_routes())
    clients = [
        HitokotoClient(
            bundle_url=BUNDLE_URL,
            http_pool=pool,
            cache_path=cache_path,
        )
        for _ in range(8)
    ]

    results = await gather(
        *(
            client.get_hitokoto((HitokotoType.ANIME,), use_cache=True)
            for client in clients
        )
    )

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

    result = await read_cached_hitokoto(cache_path, ())
    assert result.hitokoto in values
    assert await is_cache_valid(cache_path) is True
    assert list_cache_temps(cache_path) == []


@pytest.mark.parametrize("field", ["id", "uuid"])
async def test_duplicate_keys_do_not_replace_the_existing_cache(
    tmp_path: Path,
    field: str,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle("existing"))
    duplicates = bundle(include_game=True)
    duplicates[1] = duplicates[1].model_copy(
        update={field: getattr(duplicates[0], field)},
    )

    with pytest.raises(sqlite3.IntegrityError):
        await write_cache(cache_path, duplicates)

    assert (await read_cached_hitokoto(cache_path, ())).hitokoto == "existing"
    assert await is_cache_valid(cache_path) is True
    assert list_cache_temps(cache_path) == []


async def test_real_sqlite_cache_filters_types_and_rejects_empty_matches(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    sentences = bundle(include_game=True)
    await write_cache(cache_path, sentences)

    result = await read_cached_hitokoto(cache_path, (HitokotoType.GAME,))

    assert result == sentences[1]
    with pytest.raises(RuntimeError, match="no matching"):
        await read_cached_hitokoto(cache_path, (HitokotoType.JOKE,))


async def test_cache_validity_handles_missing_and_current_database(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "hitokoto.db"

    assert await is_cache_valid(cache_path) is False
    await write_cache(cache_path, bundle())
    assert await is_cache_valid(cache_path) is True


def list_cache_temps(cache_path: Path) -> list[Path]:
    return list(cache_path.parent.glob(f".{cache_path.name}.*.tmp"))


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
    await write_cache(cache_path, bundle())
    with closing(sqlite3.connect(cache_path)) as db, db:
        db.execute("UPDATE sentence SET payload = '{}'")

    assert await is_cache_valid(cache_path) is False


async def test_bundle_requires_a_sentence(tmp_path: Path) -> None:
    routes = bundle_routes()
    routes[f"{BUNDLE_URL}sentences/a.json"] = []
    client = HitokotoClient(
        bundle_url=BUNDLE_URL,
        http_pool=RecordingPool(routes),
        cache_path=tmp_path / "hitokoto.db",
    )

    with pytest.raises(ValidationError):
        await client.ensure_cache()


async def test_bundle_allows_an_empty_part_when_another_has_sentences(
    tmp_path: Path,
) -> None:
    routes = bundle_routes()
    routes[f"{BUNDLE_URL}version.json"] = {
        "sentences": [
            {"path": "./sentences/a.json"},
            {"path": "./sentences/empty.json"},
        ],
    }
    routes[f"{BUNDLE_URL}sentences/empty.json"] = []
    client = HitokotoClient(
        bundle_url=BUNDLE_URL,
        http_pool=RecordingPool(routes),
        cache_path=tmp_path / "hitokoto.db",
    )

    await client.ensure_cache()

    assert await is_cache_valid(client.cache_path)
