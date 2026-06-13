from __future__ import annotations

from asyncio import Semaphore
from datetime import date
from typing import cast

import orjson
from pydantic import JsonValue, SecretStr
from urllib3_future import AsyncPoolManager

from lst_bot.clients.klei import (
    KleiClient,
    Platform,
    Region,
    Version,
    VersionPage,
    VersionType,
)

from .support import FakePool, FakeRoutePool


def _lobby_row() -> dict[str, JsonValue]:
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


def _room_row() -> dict[str, JsonValue]:
    return {
        **_lobby_row(),
        "tick": 12_345,
        "clientmodsoff": False,
        "nat": 1,
        "desc": "A room",
    }


def _rows_payload(rows: list[JsonValue]) -> bytes:
    return orjson.dumps({"GET": rows})


def test_version_page_reads_klei_rows() -> None:
    html = """
    <h1>Don't Starve Together</h1>
    <a data-role="followButton">
        <span>Followers</span>
        <span class="ipsCommentCount">262</span>
    </a>
    <li class="cCmsRecord_row " data-rowID="2749">
        <a
            href="https://forums.kleientertainment.com/game-updates/dst/736805-r2749/"
            class="cRelease"
            data-releaseID="2749"
            data-currentRelease
        >
            <h3 class="ipsType_sectionHead ipsType_break">
                736805
                <span class="ipsBadge ipsBadge_positive">Release</span>
            </h3>
            <div class="ipsDataItem_meta">
                Released 06/11/26...
            </div>
        </a>
    </li>
    <li class="cCmsRecord_row " data-rowID="2754">
        <a
            href="https://forums.kleientertainment.com/game-updates/dst/736959-r2754/"
            class="cRelease"
            data-releaseID="2754"
            data-currentRelease
        >
            <span class="ipsType_large cUpdate_hotfix" title="Hotfix">
                <i class="fa fa-warning"></i>
            </span>
            <h3 class="ipsType_sectionHead ipsType_break">
                736959
                <span class="ipsBadge ipsBadge_positive">Release</span>
            </h3>
            <div class="ipsDataItem_meta">
                Released 06/11/26...
            </div>
        </a>
    </li>
    <ul class="ipsPagination">
        <li>Prev</li>
        <li>1</li>
        <li>2</li>
        <li>Next</li>
        <li>Page 1 of 35</li>
    </ul>
    """
    page = VersionPage.model_validate(html)
    versions = page.versions

    assert page.title == "Don't Starve Together"
    assert page.page == 1
    assert page.page_count == 35
    assert page.followers == 262
    assert versions[0].number == 736959
    assert versions[0].type is VersionType.RELEASE
    assert versions[0].date == date(2026, 6, 11)
    assert versions[0].release_id == 2754
    assert versions[0].row_id == 2754
    assert versions[0].url.endswith("/736959-r2754/")
    assert versions[0].is_current_release is True
    assert versions[0].is_hotfix is True
    assert versions[1].number == 736805
    assert versions[1].is_hotfix is False
    assert Version.parse_date("6/12/26") == date(2026, 6, 12)


def test_version_page_uses_fallbacks_and_skips_invalid_rows() -> None:
    html = """
    <title>Fallback title</title>
    <a data-role="followButton">
        <span class="ipsCommentCount">1,262</span>
    </a>
    <li class="cCmsRecord_row">missing required nodes</li>
    <li class="cCmsRecord_row " data-rowID="not-a-number">
        <a
            href="https://forums.kleientertainment.com/game-updates/dst/736959-r2754/"
            class="cRelease"
            data-releaseID="also-bad"
        >
            <h3 class="ipsType_sectionHead ipsType_break">
                736959
                <span class="ipsBadge ipsBadge_positive">Release</span>
            </h3>
            <div class="ipsDataItem_meta">
                Released 06/11/26...
            </div>
        </a>
    </li>
    """

    page = VersionPage.model_validate(html)

    assert page.title == "Fallback title"
    assert page.page is None
    assert page.page_count is None
    assert page.followers == 1262
    assert len(page.versions) == 1
    assert page.versions[0].row_id is None
    assert page.versions[0].release_id is None


async def test_klei_client_uses_injected_pool() -> None:
    pool = FakePool({"release": ["2", "10"]})
    client = KleiClient(
        access_token=SecretStr("test-value"),
        http_pool=cast(AsyncPoolManager, pool),
    )

    try:
        version = await client.get_latest_version_number()
    finally:
        await client.close()

    assert version == 10
    assert pool.cleared is True
    assert pool.calls == [
        {
            "method": "GET",
            "url": "https://s3.amazonaws.com/dstbuilds/builds.json",
            "json": None,
        },
    ]


async def test_klei_client_parses_lobby_and_room_protocol_payloads() -> None:
    pool = FakeRoutePool({
        "https://lobby.test/us-east-1-Steam.json.gz": _rows_payload([
            _lobby_row(),
            {"__rowId": "broken"},
        ]),
        "https://rooms.test/us-east-1/lobby/read": _rows_payload([
            {"__rowId": "broken"},
            _room_row(),
        ]),
    })
    credential = "test-value"
    client = KleiClient(
        lobby_url="https://lobby.test/{region}-{platform}.json.gz",
        room_url="https://rooms.test/{region}/lobby/read",
        http_pool=cast(AsyncPoolManager, pool),
        access_token=SecretStr(credential),
    )

    try:
        lobby_data = await client._get_single_lobby(
            Region.US_EAST,
            Platform.Steam,
            Semaphore(1),
        )
        room_data = await client.get_room_data([(lobby_data[0].row_id, Region.US_EAST)])
    finally:
        await client.close()

    assert len(lobby_data) == 1
    assert lobby_data[0].row_id == "row-1"
    assert lobby_data[0].region is Region.US_EAST
    assert lobby_data[0].platform is Platform.Steam
    assert lobby_data[0].connect_code == "c_connect('127.0.0.1', 10999)"
    assert len(room_data) == 1
    assert room_data[0].tick == 12_345
    assert room_data[0].desc == "A room"
    assert pool.cleared is True
    assert pool.calls == [
        {
            "method": "GET",
            "url": "https://lobby.test/us-east-1-Steam.json.gz",
            "json": None,
        },
        {
            "method": "POST",
            "url": "https://rooms.test/us-east-1/lobby/read",
            "json": {
                "__gameId": "DontStarveTogether",
                "__token": credential,
                "query": {"__rowId": "row-1"},
            },
        },
    ]
