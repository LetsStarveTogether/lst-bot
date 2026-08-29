import os
import sqlite3
from asyncio import (
    CancelledError,
    Event,
    Future,
    create_task,
    gather,
    get_running_loop,
    sleep,
    timeout,
)
from collections.abc import Mapping
from contextlib import closing
from json import dumps
from pathlib import Path
from time import time
from typing import Any, override

import hitokoto.cache as cache_module
import hitokoto.client as client_module
import pytest
from hitokoto import (
    Hitokoto,
    HitokotoClient,
)
from hitokoto.cache import (
    read_cached_hitokoto,
    write_cache,
)
from pydantic import ValidationError
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

BUNDLE_URL = "https://sentences-bundle.hitokoto.cn/"


class RecordingPool(AsyncPoolManager):
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.calls: list[dict[str, object]] = []
        self.request_options: list[dict[str, object]] = []
        self.request_started = Event()

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
        _ = body, fields, headers, json
        call: dict[str, object] = {"method": method, "url": url}
        self.calls.append(call)
        self.request_options.append(urlopen_kw)
        self.request_started.set()
        await sleep(0)
        result = self.routes[url]
        if result is None:
            return await get_running_loop().create_future()
        if isinstance(result, Future):
            result = await result
        if isinstance(result, RecordingResponse):
            return result
        payload = result if isinstance(result, bytes) else dumps(result).encode()
        return RecordingResponse(200, payload)


class RecordingResponse:
    def __init__(self, status: int, body: bytes | None = None) -> None:
        self.status = status
        self.body = body
        self.body_accessed = False
        self.closed = False
        self.read_calls: list[tuple[int | None, bool | None]] = []

    async def read(
        self,
        amount: int | None = None,
        decode_content: bool | None = None,
    ) -> bytes:
        self.body_accessed = True
        self.read_calls.append((amount, decode_content))
        if self.body is None:
            return await get_running_loop().create_future()
        return self.body if amount is None else self.body[:amount]

    async def close(self) -> None:
        self.closed = True


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
    assert response.closed is True


@pytest.mark.parametrize(
    ("body_size", "oversized"),
    [(8, False), (9, True)],
    ids=["exact", "over"],
)
async def test_client_limits_decoded_response_body(
    monkeypatch: pytest.MonkeyPatch,
    body_size: int,
    oversized: bool,
) -> None:
    limit = 8
    monkeypatch.setattr(client_module, "_MAX_HTTP_BODY_BYTES", limit)
    body = b"x" * body_size
    url = f"{BUNDLE_URL}body"
    response = RecordingResponse(200, body)
    pool = RecordingPool({url: response})
    client = HitokotoClient(http_pool=pool)
    get = client._get  # ruff: ignore[private-member-access] - focused HTTP boundary

    if oversized:
        with pytest.raises(HTTPError, match="exceeds"):
            await get(url)
    else:
        assert await get(url) == body

    assert response.read_calls == [(limit + 1, True)]
    assert response.closed is True
    assert pool.request_options == [
        {"preload_content": False, "redirect": False, "retries": False}
    ]


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
        assert route.closed is True


async def test_concurrent_reads_download_cache_once(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "hitokoto.db"
    routes = bundle_routes()
    version_url = f"{BUNDLE_URL}version.json"
    version = get_running_loop().create_future()
    routes[version_url] = version
    pool = RecordingPool(routes)
    client = HitokotoClient(
        http_pool=pool,
        cache_path=cache_path,
    )
    cancelled = create_task(client.get_hitokoto())
    surviving = create_task(client.get_hitokoto())
    await pool.request_started.wait()

    cancelled.cancel()
    with pytest.raises(CancelledError):
        await cancelled
    version.set_result(bundle_routes()[version_url])
    result = await surviving

    assert result.hitokoto == "cached hello"
    assert [call["url"] for call in pool.calls] == [
        version_url,
        f"{BUNDLE_URL}sentences/a.json",
    ]


async def test_concurrent_atomic_cache_writes(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    values = {f"sentence {index}" for index in range(8)}

    await gather(*(write_cache(cache_path, bundle(value)) for value in values))

    result = await read_cached_hitokoto(cache_path)
    assert result.hitokoto in values
    assert not any(cache_path.parent.glob(f".{cache_path.name}.*.tmp"))


async def test_real_sqlite_cache_rejects_an_empty_database(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, ())

    with pytest.raises(RuntimeError, match="no matching"):
        await read_cached_hitokoto(cache_path)


@pytest.mark.parametrize(
    "offset_seconds",
    [
        -(73 * 60 * 60),
        60 * 60,
    ],
    ids=["stale", "future"],
)
async def test_invalid_mtime_refreshes_cache(
    tmp_path: Path,
    offset_seconds: int,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle("old"))
    timestamp = time() + offset_seconds
    os.utime(cache_path, (timestamp, timestamp))
    pool = RecordingPool(bundle_routes("refreshed"))

    result = await HitokotoClient(http_pool=pool, cache_path=cache_path).get_hitokoto()

    assert result.hitokoto == "refreshed"
    assert len(pool.calls) == 2


async def test_selected_corrupt_cache_row_is_refreshed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, [*bundle("valid"), *bundle("corrupt")])
    with closing(sqlite3.connect(cache_path)) as db, db:
        db.execute("UPDATE sentence SET payload = '{}' WHERE rowid = 2")
    original_open = cache_module._open_read_only  # ruff: ignore[private-member-access]

    def open_selecting_corrupt_row(path: Path) -> sqlite3.Connection:
        db = original_open(path)
        db.create_function("random", 0, lambda: 1)
        return db

    monkeypatch.setattr(cache_module, "_open_read_only", open_selecting_corrupt_row)
    pool = RecordingPool(bundle_routes("recovered"))
    client = HitokotoClient(
        http_pool=pool,
        cache_path=cache_path,
    )

    assert (await client.get_hitokoto()).hitokoto == "recovered"
    assert len(pool.calls) == 2


async def test_stale_cache_survives_refresh_failure(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle("stale"))
    timestamp = time() - 73 * 60 * 60
    os.utime(cache_path, (timestamp, timestamp))
    pool = RecordingPool({
        f"{BUNDLE_URL}version.json": RecordingResponse(503, b"error"),
    })
    client = HitokotoClient(
        http_pool=pool,
        cache_path=cache_path,
    )

    results = await gather(*(client.get_hitokoto() for _ in range(8)))

    assert {result.hitokoto for result in results} == {"stale"}
    assert pool.calls == [{"method": "GET", "url": f"{BUNDLE_URL}version.json"}]


async def test_failed_refresh_can_retry(tmp_path: Path) -> None:
    version_url = f"{BUNDLE_URL}version.json"
    routes: dict[str, object] = {version_url: RecordingResponse(503, b"error")}
    pool = RecordingPool(routes)
    client = HitokotoClient(http_pool=pool, cache_path=tmp_path / "hitokoto.db")

    with pytest.raises(HTTPError):
        await client.get_hitokoto()
    routes.update(bundle_routes("retried"))

    assert (await client.get_hitokoto()).hitokoto == "retried"
    assert [call["url"] for call in pool.calls] == [
        version_url,
        version_url,
        f"{BUNDLE_URL}sentences/a.json",
    ]


async def test_bundle_requires_a_sentence(tmp_path: Path) -> None:
    routes = bundle_routes()
    routes[f"{BUNDLE_URL}sentences/a.json"] = []
    client = HitokotoClient(
        http_pool=RecordingPool(routes),
        cache_path=tmp_path / "hitokoto.db",
    )

    with pytest.raises(ValidationError):
        await client.get_hitokoto()


@pytest.mark.parametrize(
    "sentences",
    [
        [{"path": "./sentences/a.json"}] * 13,
        [{"path": "./sentences/../version.json"}],
    ],
    ids=["too-many-parts", "invalid-path"],
)
async def test_bundle_manifest_rejects_unofficial_parts_before_fetching(
    sentences: list[dict[str, str]],
    tmp_path: Path,
) -> None:
    version_url = f"{BUNDLE_URL}version.json"
    pool = RecordingPool({
        version_url: {
            "protocol_version": "1.0.0",
            "sentences": sentences,
        }
    })

    with pytest.raises(ValidationError):
        await HitokotoClient(
            http_pool=pool,
            cache_path=tmp_path / "hitokoto.db",
        ).get_hitokoto()

    assert pool.calls == [{"method": "GET", "url": version_url}]


async def test_bundle_allows_an_empty_part_when_another_has_sentences(
    tmp_path: Path,
) -> None:
    routes = bundle_routes()
    sentence_url = f"{BUNDLE_URL}sentences/a.json"
    routes[f"{BUNDLE_URL}version.json"] = {
        "protocol_version": "1.0.0",
        "sentences": [
            {"path": "./sentences/a.json"},
            {"path": "./sentences/b.json"},
        ],
    }
    routes[f"{BUNDLE_URL}sentences/b.json"] = []
    pool = RecordingPool(routes)
    client = HitokotoClient(
        http_pool=pool,
        cache_path=tmp_path / "hitokoto.db",
    )
    result = await client.get_hitokoto()

    assert result.hitokoto == "cached hello"
    assert [call["url"] for call in pool.calls] == [
        f"{BUNDLE_URL}version.json",
        sentence_url,
        f"{BUNDLE_URL}sentences/b.json",
    ]
