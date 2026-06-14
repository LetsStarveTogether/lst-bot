from __future__ import annotations

from asyncio import sleep
from collections.abc import Iterable
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import apsw
import pytest
from pydantic import JsonValue
from urllib3_future import AsyncPoolManager

from lst_bot.clients.hitokoto import (
    Hitokoto,
    HitokotoBundle,
    HitokotoBundleVersion,
    HitokotoClient,
    HitokotoType,
    bundle_base_url,
    bundle_file_url,
    is_cache_valid,
    read_cached_hitokoto,
    write_cache,
)

from .support import FakePool, FakeResponse, FakeRoutePool, fake_bundle_routes


class TrackingRoutePool(FakeRoutePool):
    def __init__(self, routes: dict[str, JsonValue]) -> None:
        super().__init__(routes)
        self.active_sentence_requests = 0
        self.max_sentence_requests = 0

    async def request(
        self,
        method: str,
        url: str,
        *,
        fields: Iterable[tuple[str, str]] | None = None,
        json: JsonValue = None,
    ) -> FakeResponse:
        if "/sentences/" in url:
            self.active_sentence_requests += 1
            self.max_sentence_requests = max(
                self.max_sentence_requests,
                self.active_sentence_requests,
            )
            await sleep(0)
            self.active_sentence_requests -= 1
        return await super().request(method, url, fields=fields, json=json)


def _bundle_from_routes(routes: dict[str, JsonValue]) -> HitokotoBundle:
    return HitokotoBundle.model_validate({
        "protocol_version": "1.0.0",
        "bundle_version": "1.0.1",
        "categories": routes["https://sentences-bundle.hitokoto.cn/categories.json"],
        "sentences": routes["https://sentences-bundle.hitokoto.cn/sentences/a.json"],
    })


def test_hitokoto_time_fields_parse_to_datetime_and_dump_json_iso() -> None:
    routes = fake_bundle_routes()
    bundle = _bundle_from_routes(routes)
    version = HitokotoBundleVersion.model_validate(
        routes["https://sentences-bundle.hitokoto.cn/version.json"],
    )
    sentence_time = datetime.fromtimestamp(1468605909, UTC)

    assert bundle.categories[0].created_at == datetime(
        2020,
        5,
        15,
        10,
        48,
        9,
        tzinfo=UTC,
    )
    assert bundle.categories[0].model_dump(mode="json")["created_at"] == (
        "2020-05-15T10:48:09Z"
    )
    assert bundle.sentences[0].created_at == sentence_time
    assert bundle.sentences[0].model_dump(mode="json")["created_at"] == (
        "2016-07-15T18:05:09Z"
    )
    assert version.updated_at == datetime.fromtimestamp(1781163567.796, UTC)
    assert version.model_dump(mode="json")["updated_at"] == (
        "2026-06-11T07:39:27.796000Z"
    )
    assert version.categories.timestamp == datetime.fromtimestamp(
        1597712000.881,
        UTC,
    )
    assert version.categories.model_dump(mode="json")["timestamp"] == (
        "2020-08-18T00:53:20.881000Z"
    )

    hitokoto = Hitokoto.model_validate({
        "id": 1,
        "uuid": "7bfb14e2-5538-4bde-8362-7e053f84e799",
        "hitokoto": "hello",
        "type": "a",
        "from": "source",
        "from_who": None,
        "creator": "tester",
        "creator_uid": 1,
        "reviewer": 1,
        "commit_from": "web",
        "created_at": sentence_time,
    })

    assert hitokoto.model_dump(mode="json", by_alias=True)["created_at"] == (
        "2016-07-15T18:05:09Z"
    )


async def test_hitokoto_client_uses_injected_pool() -> None:
    pool = FakePool({
        "id": 1,
        "uuid": "7bfb14e2-5538-4bde-8362-7e053f84e799",
        "hitokoto": "hello",
        "type": "a",
        "from": "source",
        "from_who": "author",
        "creator": "tester",
        "creator_uid": 1,
        "reviewer": 1,
        "commit_from": "web",
        "created_at": "2026-06-12T00:00:00",
    })
    client = HitokotoClient(
        url="https://hitokoto.example.test/",
        http_pool=cast(AsyncPoolManager, pool),
    )

    try:
        hitokoto = await client.get_hitokoto([HitokotoType.ANIME])
    finally:
        await client.close()

    assert hitokoto.hitokoto == "hello"
    assert pool.cleared is True
    assert pool.calls == [
        {
            "method": "GET",
            "url": "https://hitokoto.example.test/",
            "fields": [("c", "a")],
            "json": None,
        },
    ]


async def test_hitokoto_client_omits_type_fields_when_unfiltered() -> None:
    pool = FakePool({
        "id": 1,
        "uuid": "7bfb14e2-5538-4bde-8362-7e053f84e799",
        "hitokoto": "hello",
        "type": "a",
        "from": "source",
        "from_who": None,
        "creator": "tester",
        "creator_uid": 1,
        "reviewer": 1,
        "commit_from": "web",
        "created_at": "2026-06-12T00:00:00",
    })
    client = HitokotoClient(http_pool=cast(AsyncPoolManager, pool))

    try:
        hitokoto = await client.get_hitokoto()
    finally:
        await client.close()

    assert hitokoto.hitokoto == "hello"
    assert pool.calls == [
        {
            "method": "GET",
            "url": "https://v1.hitokoto.cn/",
            "json": None,
        },
    ]


async def test_hitokoto_client_reads_from_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / ".cache" / "hitokoto.db"
    pool = FakeRoutePool(fake_bundle_routes())
    client = HitokotoClient(
        bundle_url="sentences-bundle.hitokoto.cn",
        http_pool=cast(AsyncPoolManager, pool),
        cache_path=cache_path,
    )

    try:
        hitokoto = await client.get_hitokoto([HitokotoType.ANIME], use_cache=True)
    finally:
        await client.close()

    assert hitokoto.hitokoto == "cached hello"
    assert cache_path.is_file()
    async with aclosing(await apsw.Connection.as_async(str(cache_path))) as db:
        version_cursor = await db.execute(
            "SELECT protocol_version, bundle_version, updated_at FROM version"
        )
        version = await version_cursor.fetchone()
        category_cursor = await db.execute(
            "SELECT created_at, updated_at FROM category"
        )
        category_times = await category_cursor.fetchone()
        sentence_cursor = await db.execute("SELECT created_at FROM sentence")
        sentence_time = await sentence_cursor.fetchone()
        category_count = await (await db.execute("SELECT COUNT(*) FROM category")).get
        sentence_count = await (await db.execute("SELECT COUNT(*) FROM sentence")).get
    assert version is not None
    assert version[:2] == ("1.0.0", "1.0.1")
    assert isinstance(version[2], str)
    assert datetime.fromisoformat(version[2]).tzinfo is UTC
    assert category_times == (
        "2020-05-15T10:48:09+00:00",
        "2020-05-15T10:48:12+00:00",
    )
    assert sentence_time == ("2016-07-15T18:05:09+00:00",)
    assert category_count == 1
    assert sentence_count == 1
    assert pool.cleared is True
    assert [call["url"] for call in pool.calls] == [
        "https://sentences-bundle.hitokoto.cn/version.json",
        "https://sentences-bundle.hitokoto.cn/categories.json",
        "https://sentences-bundle.hitokoto.cn/sentences/a.json",
    ]


async def test_hitokoto_client_downloads_sentence_files_concurrently() -> None:
    routes = fake_bundle_routes("first")
    base_url = "https://sentences-bundle.hitokoto.cn/"
    version = cast(dict[str, JsonValue], routes[f"{base_url}version.json"])
    version["sentences"] = [
        *cast(list[dict[str, JsonValue]], version["sentences"]),
        {
            "name": "游戏",
            "key": "c",
            "path": "./sentences/c.json",
            "timestamp": 1619244060706,
        },
    ]
    routes[f"{base_url}sentences/c.json"] = [
        {
            "id": 3,
            "uuid": "0ed43f7f-7af4-4f06-8665-101855d66d74",
            "hitokoto": "second",
            "type": "c",
            "from": "game source",
            "from_who": None,
            "creator": "tester",
            "creator_uid": 1,
            "reviewer": 1,
            "commit_from": "web",
            "created_at": "1468605909",
            "length": 6,
        },
    ]
    pool = TrackingRoutePool(routes)
    client = HitokotoClient(http_pool=cast(AsyncPoolManager, pool))

    try:
        bundle = await client._download_bundle()
    finally:
        await client.close()

    assert pool.max_sentence_requests == 2
    assert [sentence.hitokoto for sentence in bundle.sentences] == [
        "first",
        "second",
    ]


async def test_hitokoto_client_ensures_cache_on_enter(tmp_path: Path) -> None:
    cache_path = tmp_path / ".cache" / "hitokoto.db"
    pool = FakeRoutePool(fake_bundle_routes())
    client = HitokotoClient(
        http_pool=cast(AsyncPoolManager, pool),
        cache_path=cache_path,
        download_cache_on_enter=True,
    )

    async with client:
        assert cache_path.is_file()

    assert pool.cleared is True


async def test_hitokoto_client_keeps_valid_cache_on_enter(tmp_path: Path) -> None:
    cache_path = tmp_path / ".cache" / "hitokoto.db"
    routes = fake_bundle_routes("already cached")
    await write_cache(cache_path, _bundle_from_routes(routes))
    pool = FakeRoutePool(routes)
    client = HitokotoClient(
        http_pool=cast(AsyncPoolManager, pool),
        cache_path=cache_path,
        download_cache_on_enter=True,
    )

    async with client:
        hitokoto = await client.get_hitokoto([HitokotoType.ANIME], use_cache=True)

    assert hitokoto.hitokoto == "already cached"
    assert pool.calls == []
    assert pool.cleared is True


async def test_hitokoto_cache_validity_rejects_missing_stale_and_bad_version(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / ".cache" / "hitokoto.db"

    assert await is_cache_valid(cache_path) is False

    await write_cache(cache_path, _bundle_from_routes(fake_bundle_routes()))

    assert await is_cache_valid(cache_path) is True

    async with aclosing(await apsw.Connection.as_async(str(cache_path))) as db:
        await db.execute(
            "UPDATE version SET updated_at = ?",
            ((datetime.now(UTC) - timedelta(hours=73)).isoformat(),),
        )

    assert await is_cache_valid(cache_path) is False

    async with aclosing(await apsw.Connection.as_async(str(cache_path))) as db:
        await db.execute("UPDATE version SET updated_at = ?", ("not-a-date",))

    assert await is_cache_valid(cache_path) is False


async def test_read_cached_hitokoto_filters_types_and_reports_empty_match(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / ".cache" / "hitokoto.db"
    routes = fake_bundle_routes()
    category_url = "https://sentences-bundle.hitokoto.cn/categories.json"
    sentence_url = "https://sentences-bundle.hitokoto.cn/sentences/a.json"
    categories = [
        *cast(list[dict[str, JsonValue]], routes[category_url]),
        {
            "id": 3,
            "name": "游戏",
            "desc": "Game - 游戏",
            "key": "c",
            "created_at": "2020-05-15T10:48:09Z",
            "updated_at": "2020-05-15T10:48:12Z",
            "path": "./sentences/c.json",
        },
    ]
    sentences = [
        *cast(list[dict[str, JsonValue]], routes[sentence_url]),
        {
            "id": 3,
            "uuid": "0ed43f7f-7af4-4f06-8665-101855d66d74",
            "hitokoto": "cached game",
            "type": "c",
            "from": "game source",
            "from_who": None,
            "creator": "tester",
            "creator_uid": 1,
            "reviewer": 1,
            "commit_from": "web",
            "created_at": "1468605909",
            "length": 11,
        },
    ]
    await write_cache(
        cache_path,
        HitokotoBundle.model_validate({
            "protocol_version": "1.0.0",
            "bundle_version": "1.0.1",
            "categories": categories,
            "sentences": sentences,
        }),
    )

    hitokoto = await read_cached_hitokoto(cache_path, (HitokotoType.GAME,))

    assert hitokoto.hitokoto == "cached game"
    assert hitokoto.type is HitokotoType.GAME
    with pytest.raises(RuntimeError, match="no matching"):
        await read_cached_hitokoto(cache_path, (HitokotoType.JOKE,))


def test_bundle_url_helpers_normalize_base_and_file_paths() -> None:
    base_url = bundle_base_url(
        "https://sentences-bundle.hitokoto.cn/api?unused=1#fragment",
    )

    assert base_url.url == "https://sentences-bundle.hitokoto.cn/api/"
    assert bundle_file_url(base_url, "./sentences/a.json") == (
        "https://sentences-bundle.hitokoto.cn/api/sentences/a.json"
    )
    assert bundle_file_url(base_url, "/version.json") == (
        "https://sentences-bundle.hitokoto.cn/api/version.json"
    )
    with pytest.raises(RuntimeError, match="empty"):
        bundle_base_url(" ")
