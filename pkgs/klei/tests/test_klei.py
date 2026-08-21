import json as jsonlib
from asyncio import Event, TaskGroup, timeout
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, cast, override
from unittest.mock import Mock

import klei.client as client_module
import pytest
from klei import (
    KleiClient,
    LobbyData,
    Platform,
    RoomData,
    Secondary,
    VersionType,
)
from klei.models import KleiDataResponse
from pydantic import JsonValue, SecretStr, ValidationError
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

VERSION_URL = "https://forum.example.test/versions/"
LOBBY_URL = "https://lobby.example.test/{region}-{platform}.json.gz"
ROOM_URL = "https://rooms.example.test/{region}/lobby/read"

VERSION_HTML = """
<li class="cCmsRecord_row">
  <a class="cRelease">
    <h3 class="ipsType_sectionHead">
      736805 <span class="ipsBadge">Release</span>
    </h3>
    <div class="ipsDataItem_meta">Released 06/10/26</div>
  </a>
</li>
<li class="cCmsRecord_row">invalid row</li>
<li class="cCmsRecord_row">
  <a class="cRelease">
    <h3 class="ipsType_sectionHead">
      736959 <span class="ipsBadge">Release</span>
    </h3>
    <div class="ipsDataItem_meta">Released 06/11/26</div>
  </a>
</li>
"""


@dataclass(frozen=True)
class Reply:
    body: bytes
    status: int = 200
    release: Event | None = None
    body_release: Event | None = None


class Response:
    def __init__(self, body: bytes, status: int, body_release: Event | None) -> None:
        self.status = status
        self._body = body
        self._body_release = body_release
        self.body_accessed = False

    @property
    def data(self) -> Awaitable[bytes]:
        self.body_accessed = True
        return self._read()

    async def _read(self) -> bytes:
        if self._body_release is not None:
            await self._body_release.wait()
        return self._body


class RecordingPool:
    def __init__(self, routes: Mapping[str, bytes | Reply]) -> None:
        self.routes = routes
        self.calls: list[dict[str, object]] = []
        self.responses: list[Response] = []
        self.cleared = False

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        call: dict[str, object] = {"method": method, "url": url}
        if (json := kwargs.get("json")) is not None:
            call["json"] = json
        self.calls.append(call)

        result = self.routes[url]
        reply = result if isinstance(result, Reply) else Reply(result)
        if reply.release is not None:
            await reply.release.wait()
        response = Response(reply.body, reply.status, reply.body_release)
        self.responses.append(response)
        return response

    async def clear(self) -> None:
        self.cleared = True


class BlockingPool(RecordingPool):
    def __init__(self, routes: Mapping[str, bytes | Reply], limit: int) -> None:
        super().__init__(routes)
        self.limit = limit
        self.active = 0
        self.max_active = 0
        self.saturated = Event()
        self.release = Event()

    @override
    async def request(self, *args: Any, **kwargs: Any) -> Any:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == self.limit:
            self.saturated.set()
        try:
            await self.release.wait()
            return await super().request(*args, **kwargs)
        finally:
            self.active -= 1


def lobby_row(
    row_id: str = "row-1",
    *,
    name: str = "DST cluster",
) -> dict[str, JsonValue]:
    return {
        "__rowId": row_id,
        "__addr": "127.0.0.1",
        "name": name,
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
        "secondaries": {"1": {"id": "1", "port": 11000, "__addr": "127.0.0.2"}},
    }


def room_row(
    row_id: str = "row-1", *, name: str = "DST cluster"
) -> dict[str, JsonValue]:
    return {
        **lobby_row(row_id, name=name),
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
    room_concurrency: int = 24,
    http_timeout: float = 1.0,
) -> KleiClient:
    return KleiClient(
        access_token=SecretStr("test-token"),
        version_url=VERSION_URL,
        lobby_url=LOBBY_URL,
        room_url=ROOM_URL,
        lobby_concurrency=lobby_concurrency,
        room_concurrency=room_concurrency,
        http_timeout=http_timeout,
        http_pool=cast("AsyncPoolManager", pool),
    )


async def test_client_reads_only_consumed_version_fields() -> None:
    pool = RecordingPool({VERSION_URL: VERSION_HTML.encode()})

    versions = await client(pool).get_latest_versions()

    assert [version.number for version in versions] == [736959, 736805]
    assert versions[0].type is VersionType.RELEASE
    assert versions[0].date == date(2026, 6, 11)
    assert set(versions[0].model_dump()) == {"number", "type", "date"}
    assert pool.calls == [
        {
            "method": "GET",
            "url": VERSION_URL,
        }
    ]


async def test_client_parses_dynamic_region_lobby_and_room() -> None:
    region = "sa-east-1"
    lobby_url = LOBBY_URL.format(region=region, platform=Platform.Steam.name)
    room_url = ROOM_URL.format(region=region)
    pool = RecordingPool({
        lobby_url: rows_payload([
            lobby_row() | {"season": "mild"},
            {"__rowId": "invalid"},
        ]),
        room_url: rows_payload([{"__rowId": "invalid"}, room_row()]),
    })

    value = client(pool)
    lobbies = await value.get_lobby_data(
        regions=(region,),
        platforms=(Platform.Steam,),
    )
    rooms = await value.get_room_data(((lobbies[0].row_id, region),))

    assert len(lobbies) == 1
    assert lobbies[0].region == region
    assert lobbies[0].platform is Platform.Steam
    assert lobbies[0].season == "mild"
    assert len(rooms) == 1
    assert rooms[0].tick == 12_345
    assert pool.calls[1]["json"] == {
        "__gameId": "DontStarveTogether",
        "__token": "test-token",
        "query": {"__rowId": "row-1"},
    }


async def test_non_success_http_status_fails_before_parsing_body() -> None:
    pool = RecordingPool({VERSION_URL: Reply(VERSION_HTML.encode(), status=500)})

    with pytest.raises(HTTPError, match="HTTP 500"):
        await client(pool).get_latest_versions()

    assert pool.responses[0].body_accessed is False


@pytest.mark.parametrize("stage", ["request", "body"])
async def test_request_has_wall_clock_timeout(stage: str) -> None:
    blocked = Event()
    pool = RecordingPool({
        VERSION_URL: Reply(
            VERSION_HTML.encode(),
            release=blocked if stage == "request" else None,
            body_release=blocked if stage == "body" else None,
        )
    })

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await client(pool, http_timeout=0.01).get_latest_versions()


async def test_client_clears_only_its_own_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrowed = RecordingPool({})
    async with client(borrowed):
        pass
    assert borrowed.cleared is False

    owned = RecordingPool({})
    monkeypatch.setattr(client_module, "AsyncPoolManager", Mock(return_value=owned))
    async with KleiClient(SecretStr("token")):
        pass
    assert owned.cleared is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lobby_concurrency": 0},
        {"room_concurrency": 0},
        {"lobby_concurrency": True},
        {"room_concurrency": 1.0},
        {"http_timeout": 0},
        {"http_timeout": float("nan")},
        {"http_timeout": float("inf")},
    ],
)
def test_client_limits_are_strict_positive_finite(kwargs: Any) -> None:
    with pytest.raises(ValidationError):
        KleiClient(SecretStr("token"), **kwargs)


async def test_client_strictly_validates_platform_filters() -> None:
    value = client(RecordingPool({}))

    with pytest.raises(ValidationError):
        await value.get_lobby_data(platforms=("Steam",))  # ty: ignore[invalid-argument-type]


async def test_client_rejects_combined_platform_filters() -> None:
    value = client(RecordingPool({}))

    with pytest.raises(ValidationError, match="require one platform"):
        await value.get_lobby_data(platforms=(Platform.Steam | Platform.PSN,))


async def test_lobby_limit_is_global_across_concurrent_batches() -> None:
    regions = ("us-east-1", "eu-central-1", "sa-east-1", "ca-central-1")
    routes: dict[str, bytes] = {
        LOBBY_URL.format(region=region, platform=Platform.Steam.name): rows_payload([])
        for region in regions
    }
    pool = BlockingPool(routes, limit=2)

    async with client(pool, lobby_concurrency=2) as value, TaskGroup() as tasks:
        first = tasks.create_task(
            value.get_lobby_data(
                regions=regions[:2],
                platforms=(Platform.Steam,),
            )
        )
        second = tasks.create_task(
            value.get_lobby_data(
                regions=regions[2:],
                platforms=(Platform.Steam,),
            )
        )
        async with timeout(1):
            await pool.saturated.wait()
        assert pool.max_active == 2
        pool.release.set()

    assert first.result() == second.result() == []
    assert len(pool.calls) == 4


def test_response_envelope_and_lobby_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        KleiDataResponse[LobbyData].model_validate({})

    internal = LobbyData.model_validate_json(
        jsonlib.dumps(lobby_row() | {"platform": 19}),
        context={"region": "us-east-1"},
    )
    assert internal.platform.value == 19

    for changes in (
        {"port": 0},
        {"port": 65536},
        {"connected": -1},
        {"maxconnections": -1},
        {"connected": 7},
        {"v": "736959"},
        {"v": -1},
        {"allownewplayers": 1},
        {"__addr": True},
        {"__addr": 2130706433},
        {"platform": True},
        {"secondaries": {"1": {"id": "1", "__addr": True}}},
        {"secondaries": {"1": {"id": "1", "__addr": 2130706433}}},
    ):
        with pytest.raises(ValidationError):
            LobbyData.model_validate_json(
                jsonlib.dumps(lobby_row() | changes),
                context={"region": "us-east-1"},
            )

    with pytest.raises(ValidationError):
        Secondary.model_validate({"id": "secondary", "port": 0})

    for changes in ({"tick": "12345"}, {"clientmodsoff": 0}, {"nat": "1"}):
        with pytest.raises(ValidationError):
            RoomData.model_validate_json(
                jsonlib.dumps(room_row() | changes),
                context={"region": "us-east-1"},
            )
