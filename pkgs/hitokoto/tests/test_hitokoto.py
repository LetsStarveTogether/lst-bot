import os
import sqlite3
from asyncio import (
    CancelledError,
    Event,
    Future,
    Task,
    create_task,
    gather,
    get_running_loop,
    sleep,
    timeout,
)
from collections.abc import AsyncIterator, Mapping
from contextlib import closing
from gc import collect
from gzip import compress
from pathlib import Path
from time import time
from typing import override

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
from httpx2 import (
    AsyncByteStream,
    AsyncClient,
    HTTPError,
    MockTransport,
    Request,
    Response,
)
from pydantic import ValidationError

BUNDLE_URL = "https://sentences-bundle.hitokoto.cn/"


class MockAPI:
    def __init__(self, routes: Mapping[str, object]) -> None:
        self.routes = routes
        self.requests: list[Request] = []
        self.started = Event()
        self.http_client = AsyncClient(
            transport=MockTransport(self), trust_env=False, follow_redirects=True
        )

    async def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        self.started.set()
        result = self.routes[str(request.url)]
        if result is None:
            return await get_running_loop().create_future()
        if isinstance(result, Future):
            result = await result
        if isinstance(result, Response):
            return result
        return (
            Response(200, content=result)
            if isinstance(result, bytes)
            else Response(200, json=result)
        )


class BodyStream(AsyncByteStream):
    def __init__(self, body: bytes | None = None) -> None:
        self.body = body
        self.accessed = False
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.accessed = True
        if self.body is None:
            await get_running_loop().create_future()
        else:
            yield self.body

    @override
    async def aclose(self) -> None:
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


@pytest.mark.parametrize("status", [201, 302, 500])
async def test_client_consumes_error_body_before_failing(
    tmp_path: Path, status: int
) -> None:
    body = BodyStream(b"error")
    response = Response(status, headers={"Location": BUNDLE_URL}, stream=body)
    api = MockAPI({f"{BUNDLE_URL}version.json": response})

    with pytest.raises(HTTPError, match=f"HTTP {status}"):
        await HitokotoClient(
            http_client=api.http_client,
            cache_path=tmp_path / "hitokoto.db",
        ).get_hitokoto()

    assert body.accessed
    assert body.closed
    assert response.is_closed


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
    stream = BodyStream(compress(body))
    response = Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
    api = MockAPI({url: response})
    client = HitokotoClient(http_client=api.http_client)
    get = client._get  # ruff: ignore[private-member-access] - focused HTTP boundary

    if oversized:
        with pytest.raises(HTTPError, match="exceeds"):
            await get(url)
    else:
        assert await get(url) == body

    assert stream.accessed
    assert stream.closed
    assert response.is_closed
    assert api.requests[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), client_module.HTTP_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    "stage",
    ["request", "body"],
)
async def test_client_applies_wall_clock_timeout_to_all_io(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(client_module, "HTTP_TIMEOUT_SECONDS", 0.001)
    body = BodyStream()
    response = Response(200, stream=body)
    api = MockAPI({
        f"{BUNDLE_URL}version.json": None if stage == "request" else response
    })

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await HitokotoClient(
                http_client=api.http_client,
                cache_path=tmp_path / "hitokoto.db",
            ).get_hitokoto()

    if stage == "body":
        assert body.accessed
        assert body.closed
        assert response.is_closed


async def test_concurrent_reads_download_cache_once(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "hitokoto.db"
    routes = bundle_routes()
    version_url = f"{BUNDLE_URL}version.json"
    version = get_running_loop().create_future()
    routes[version_url] = version
    api = MockAPI(routes)
    client = HitokotoClient(
        http_client=api.http_client,
        cache_path=cache_path,
    )
    cancelled = create_task(client.get_hitokoto())
    surviving = create_task(client.get_hitokoto())
    await api.started.wait()

    cancelled.cancel()
    with pytest.raises(CancelledError):
        await cancelled
    version.set_result(bundle_routes()[version_url])
    result = await surviving

    assert result.hitokoto == "cached hello"
    assert [str(request.url) for request in api.requests] == [
        version_url,
        f"{BUNDLE_URL}sentences/a.json",
    ]


async def test_cancelled_sole_read_consumes_failed_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    version_url = f"{BUNDLE_URL}version.json"
    loop = get_running_loop()
    version = loop.create_future()
    api = MockAPI({version_url: version})
    client = HitokotoClient(
        http_client=api.http_client, cache_path=tmp_path / "hitokoto.db"
    )
    reports: list[dict[str, object]] = []
    monkeypatch.setattr(loop, "call_exception_handler", reports.append)
    caller = create_task(client.get_hitokoto())
    await api.started.wait()
    refresh = client._refresh_task  # ruff: ignore[private-member-access] - lifecycle regression
    assert refresh is not None
    completed = Event()
    refresh.add_done_callback(lambda _task: completed.set())

    caller.cancel()
    with pytest.raises(CancelledError):
        await caller
    version.set_result(Response(503, content=b"error"))
    await completed.wait()
    del caller, refresh, client
    collect()
    await sleep(0)

    assert reports == []


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_context_exit_cancels_and_waits_for_shared_refresh(
    tmp_path: Path,
    cancel_caller: bool,
) -> None:
    body = BodyStream()
    response = Response(200, stream=body)
    api = MockAPI({f"{BUNDLE_URL}version.json": response})
    client = HitokotoClient(
        http_client=api.http_client, cache_path=tmp_path / "hitokoto.db"
    )
    callers: list[Task[Hitokoto]] = []

    async def use_client() -> None:
        async with client:
            caller = create_task(client.get_hitokoto())
            callers.append(caller)
            await api.started.wait()
            if cancel_caller:
                caller.cancel()
                with pytest.raises(CancelledError):
                    await caller
            raise CancelledError

    async with timeout(1):
        with pytest.raises(CancelledError):
            await use_client()
        with pytest.raises(CancelledError):
            await callers[0]
        await client.close()

    assert response.is_closed
    assert body.closed
    assert not (tmp_path / "hitokoto.db").exists()


@pytest.mark.parametrize("already_cancelling", [False, True])
@pytest.mark.parametrize("stage", ["body", "close"])
async def test_close_drains_response_even_when_repeatedly_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_cancelling: bool,
    stage: str,
) -> None:
    body = BodyStream(b"{}" if stage == "close" else None)
    response = Response(200, stream=body)
    api = MockAPI({f"{BUNDLE_URL}version.json": response})
    client = HitokotoClient(
        http_client=api.http_client, cache_path=tmp_path / "hitokoto.db"
    )
    close_started = Event()
    release_close = Event()

    async def close_response() -> None:
        close_started.set()
        await release_close.wait()
        body.closed = True

    monkeypatch.setattr(body, "aclose", close_response)
    async with timeout(1):
        caller = create_task(client.get_hitokoto())
        await api.started.wait()
        if stage == "close":
            await close_started.wait()
        if already_cancelling:
            refresh = client._refresh_task  # ruff: ignore[private-member-access] - cancellation lifecycle
            assert refresh is not None
            refresh.cancel()
            await close_started.wait()
        closer = create_task(client.close())
        await close_started.wait()
        await sleep(0)
        try:
            for _ in range(2):
                closer.cancel()
                await sleep(0)
                await sleep(0)
                assert not closer.done()
        finally:
            release_close.set()
            results = await gather(caller, closer, return_exceptions=True)

    assert all(isinstance(result, CancelledError) for result in results)
    assert response.is_closed
    assert body.closed


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
    api = MockAPI(bundle_routes("refreshed"))

    result = await HitokotoClient(
        http_client=api.http_client, cache_path=cache_path
    ).get_hitokoto()

    assert result.hitokoto == "refreshed"
    assert len(api.requests) == 2


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
    api = MockAPI(bundle_routes("recovered"))
    client = HitokotoClient(
        http_client=api.http_client,
        cache_path=cache_path,
    )

    assert (await client.get_hitokoto()).hitokoto == "recovered"
    assert len(api.requests) == 2


async def test_stale_cache_survives_refresh_failure(tmp_path: Path) -> None:
    cache_path = tmp_path / "hitokoto.db"
    await write_cache(cache_path, bundle("stale"))
    timestamp = time() - 73 * 60 * 60
    os.utime(cache_path, (timestamp, timestamp))
    api = MockAPI({
        f"{BUNDLE_URL}version.json": Response(503, content=b"error"),
    })
    client = HitokotoClient(
        http_client=api.http_client,
        cache_path=cache_path,
    )

    results = await gather(*(client.get_hitokoto() for _ in range(8)))

    assert {result.hitokoto for result in results} == {"stale"}
    assert [(request.method, str(request.url)) for request in api.requests] == [
        ("GET", f"{BUNDLE_URL}version.json")
    ]


async def test_failed_refresh_can_retry(tmp_path: Path) -> None:
    version_url = f"{BUNDLE_URL}version.json"
    routes: dict[str, object] = {version_url: Response(503, content=b"error")}
    api = MockAPI(routes)
    client = HitokotoClient(
        http_client=api.http_client, cache_path=tmp_path / "hitokoto.db"
    )

    with pytest.raises(HTTPError):
        await client.get_hitokoto()
    routes.update(bundle_routes("retried"))

    assert (await client.get_hitokoto()).hitokoto == "retried"
    assert [str(request.url) for request in api.requests] == [
        version_url,
        version_url,
        f"{BUNDLE_URL}sentences/a.json",
    ]


async def test_bundle_requires_a_sentence(tmp_path: Path) -> None:
    routes = bundle_routes()
    routes[f"{BUNDLE_URL}sentences/a.json"] = []
    client = HitokotoClient(
        http_client=MockAPI(routes).http_client,
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
    api = MockAPI({
        version_url: {
            "protocol_version": "1.0.0",
            "sentences": sentences,
        }
    })

    with pytest.raises(ValidationError):
        await HitokotoClient(
            http_client=api.http_client,
            cache_path=tmp_path / "hitokoto.db",
        ).get_hitokoto()

    assert [(request.method, str(request.url)) for request in api.requests] == [
        ("GET", version_url)
    ]


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
    api = MockAPI(routes)
    client = HitokotoClient(
        http_client=api.http_client,
        cache_path=tmp_path / "hitokoto.db",
    )
    result = await client.get_hitokoto()

    assert result.hitokoto == "cached hello"
    assert [str(request.url) for request in api.requests] == [
        f"{BUNDLE_URL}version.json",
        sentence_url,
        f"{BUNDLE_URL}sentences/b.json",
    ]
