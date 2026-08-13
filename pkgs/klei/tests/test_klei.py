from __future__ import annotations

import json as jsonlib
from asyncio import Event, TaskGroup, timeout
from collections.abc import Mapping
from datetime import date
from typing import Any, override

import pytest
from klei import (
    KleiClient,
    Platform,
    Region,
    Version,
    VersionPage,
    VersionType,
)
from pydantic import JsonValue, SecretStr, ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError

BUILD_URL = "https://build.example.test/versions.json"
VERSION_URL = "https://forum.example.test/versions/"
REGION_URL = "https://lobby.example.test/regions.json"
LOBBY_URL = "https://lobby.example.test/{region}-{platform}.json.gz"
ROOM_URL = "https://rooms.example.test/{region}/lobby/read"

VERSION_HTML = """
<h1>Don't Starve Together</h1>
<a data-role="followButton"><span class="ipsCommentCount">262</span></a>
<li class="cCmsRecord_row " data-rowID="2749">
  <a href="https://forum.example.test/736805-r2749/"
     class="cRelease" data-releaseID="2749" data-currentRelease>
    <h3 class="ipsType_sectionHead ipsType_break">
      736805 <span class="ipsBadge ipsBadge_positive">Release</span>
    </h3>
    <div class="ipsDataItem_meta">Released 06/11/26</div>
  </a>
</li>
<li class="cCmsRecord_row " data-rowID="2754">
  <a href="https://forum.example.test/736959-r2754/"
     class="cRelease" data-releaseID="2754" data-currentRelease>
    <span class="ipsType_large cUpdate_hotfix" title="Hotfix"></span>
    <h3 class="ipsType_sectionHead ipsType_break">
      736959 <span class="ipsBadge ipsBadge_positive">Release</span>
    </h3>
    <div class="ipsDataItem_meta">Released 06/11/26</div>
  </a>
</li>
<ul class="ipsPagination"><li>Page 1 of 35</li></ul>
"""


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


class BlockingPool(RecordingPool):
    def __init__(self, routes: Mapping[str, object], limit: int) -> None:
        super().__init__(routes)
        self.limit = limit
        self.active = 0
        self.max_active = 0
        self.saturated = Event()
        self.release = Event()

    async def request(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == self.limit:
            self.saturated.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return await super().request(*args, **kwargs)


def lobby_row() -> dict[str, JsonValue]:
    return {
        "__rowId": "row-1",
        "__addr": "127.0.0.1",
        "name": "DST cluster",
        "port": 10999,
        "host": "host-ku",
        "connected": 3,
        "maxconnections": 6,
        "v": 736959,
        "allownewplayers": True,
        "clanonly": False,
        "clienthosted": False,
        "dedicated": True,
        "fo": False,
        "lanonly": False,
        "mods": True,
        "password": False,
        "pvp": False,
        "serverpaused": False,
        "platform": 1,
        "session": "session-id",
        "guid": "guid",
        "intent": "social",
        "steamroom": "steam-room",
    }


def room_row() -> dict[str, JsonValue]:
    return {
        **lobby_row(),
        "tick": 12_345,
        "clientmodsoff": False,
        "nat": 1,
        "desc": "A room",
    }


def rows_payload(rows: list[JsonValue]) -> bytes:
    return jsonlib.dumps({"GET": rows}).encode()


def client(
    pool: RecordingPool,
    *,
    lobby_concurrency: int = 8,
) -> KleiClient:
    return KleiClient(
        access_token=SecretStr("test-token"),
        build_url=BUILD_URL,
        version_url=VERSION_URL,
        region_url=REGION_URL,
        lobby_url=LOBBY_URL,
        room_url=ROOM_URL,
        lobby_concurrency=lobby_concurrency,
        http_pool=pool,
    )


def test_version_page_parses_and_orders_valid_rows() -> None:
    page = VersionPage.model_validate(VERSION_HTML)

    assert page.title == "Don't Starve Together"
    assert page.page == 1
    assert page.page_count == 35
    assert page.followers == 262
    assert [version.number for version in page.versions] == [736959, 736805]
    assert page.versions[0].type is VersionType.RELEASE
    assert page.versions[0].date == date(2026, 6, 11)
    assert page.versions[0].release_id == 2754
    assert page.versions[0].row_id == 2754
    assert page.versions[0].is_current_release is True
    assert page.versions[0].is_hotfix is True
    assert Version.parse_date("6/12/26") == date(2026, 6, 12)


def test_version_page_uses_fallbacks_and_omits_invalid_rows() -> None:
    page = VersionPage.model_validate("""
        <title>Fallback title</title>
        <a data-role="followButton"><span class="ipsCommentCount">1,262</span></a>
        <li class="cCmsRecord_row">missing required nodes</li>
        <li class="cCmsRecord_row " data-rowID="not-a-number">
          <a href="https://forum.example.test/736959-r2754/" class="cRelease"
             data-releaseID="also-bad">
            <h3 class="ipsType_sectionHead ipsType_break">
              736959 <span class="ipsBadge ipsBadge_positive">Release</span>
            </h3>
            <div class="ipsDataItem_meta">Released 06/11/26</div>
          </a>
        </li>
    """)

    assert page.title == "Fallback title"
    assert page.page is page.page_count is None
    assert page.followers == 1262
    assert len(page.versions) == 1
    assert page.versions[0].row_id is page.versions[0].release_id is None


async def test_client_reads_public_metadata_endpoints() -> None:
    pool = RecordingPool({
        BUILD_URL: {"release": ["2", "10"]},
        VERSION_URL: VERSION_HTML.encode(),
        REGION_URL: {"LobbyRegions": [{"Region": "us-east-1"}, {"Region": "eu"}]},
    })

    async with client(pool) as value:
        latest = await value.get_latest_version_number()
        page = await value.get_version_page()
        versions = await value.get_latest_versions()
        regions = await value.get_regions()

    assert latest == 10
    assert page.versions == versions
    assert regions == ["us-east-1", "eu"]
    assert [call["url"] for call in pool.calls] == [
        BUILD_URL,
        VERSION_URL,
        VERSION_URL,
        REGION_URL,
    ]
    assert pool.cleared is True


async def test_client_parses_lobby_and_room_payloads_through_public_api() -> None:
    lobby_url = LOBBY_URL.format(region=Region.US_EAST, platform=Platform.Steam.name)
    room_url = ROOM_URL.format(region=Region.US_EAST)
    pool = RecordingPool({
        lobby_url: rows_payload([lobby_row(), {"__rowId": "invalid"}]),
        room_url: rows_payload([{"__rowId": "invalid"}, room_row()]),
    })

    async with client(pool) as value:
        lobbies = await value.get_lobby_data(
            regions=(Region.US_EAST,),
            platforms=(Platform.Steam,),
        )
        rooms = await value.get_room_data(((lobbies[0].row_id, Region.US_EAST),))

    assert len(lobbies) == 1
    assert lobbies[0].region is Region.US_EAST
    assert lobbies[0].platform is Platform.Steam
    assert lobbies[0].connect_code == "c_connect('127.0.0.1', 10999)"
    assert len(rooms) == 1
    assert rooms[0].tick == 12_345
    assert rooms[0].desc == "A room"
    assert pool.calls[1]["json"] == {
        "__gameId": "DontStarveTogether",
        "__token": "test-token",
        "query": {"__rowId": "row-1"},
    }
    assert pool.cleared is True


@pytest.mark.parametrize("resource", ["lobby", "room"], ids=["lobby", "room"])
async def test_client_omits_http_failures_and_closes_pool(resource: str) -> None:
    lobby_url = LOBBY_URL.format(region=Region.US_EAST, platform=Platform.Steam.name)
    room_url = ROOM_URL.format(region=Region.US_EAST)
    pool = RecordingPool({
        lobby_url: HTTPError("lobby offline"),
        room_url: HTTPError("room offline"),
    })

    async with client(pool) as value:
        if resource == "lobby":
            result = await value.get_lobby_data(
                regions=(Region.US_EAST,),
                platforms=(Platform.Steam,),
            )
        else:
            result = await value.get_room_data((("row-1", Region.US_EAST),))

    assert result == []
    assert pool.cleared is True


async def test_client_propagates_invalid_payload_and_closes_pool() -> None:
    pool = RecordingPool({BUILD_URL: b"not-json"})

    with pytest.raises(ValidationError):
        async with client(pool) as value:
            await value.get_latest_version_number()

    assert pool.cleared is True


async def test_lobby_concurrency_never_exceeds_configured_limit() -> None:
    regions = (Region.US_EAST, Region.EU_CENTRAL)
    platforms = (Platform.Steam, Platform.PSN)
    routes = {
        LOBBY_URL.format(region=region, platform=platform.name): rows_payload([])
        for region in regions
        for platform in platforms
    }
    pool = BlockingPool(routes, limit=2)

    async with client(pool, lobby_concurrency=2) as value, TaskGroup() as tasks:
        task = tasks.create_task(
            value.get_lobby_data(regions=regions, platforms=platforms),
        )
        async with timeout(1):
            await pool.saturated.wait()
        assert pool.max_active == 2
        pool.release.set()

    assert task.result() == []
    assert len(pool.calls) == 4
    assert pool.cleared is True
