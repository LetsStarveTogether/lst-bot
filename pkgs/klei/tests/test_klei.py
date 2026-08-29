import json as jsonlib
from asyncio import Event, create_task, sleep, timeout
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import klei.client as client_module
import pytest
from klei import (
    KleiClient,
    LobbyData,
    RoomData,
    VersionType,
)
from klei.models import KleiDataResponse
from pydantic import JsonValue, SecretStr, ValidationError
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

VERSION_URL = "https://kleiforums.com/game-updates/dst/"
LOBBY_URL = "https://lobby-v2-cdn.klei.com/{region}-Steam.json.gz"
ROOM_URL = "https://lobby-v2-{region}.klei.com/lobby/read"
REGIONS = ("us-east-1", "eu-central-1", "ap-southeast-1", "ap-east-1")

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

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> Any:
        call: dict[str, object] = {
            "method": method,
            "url": url,
            "redirect": kwargs.get("redirect"),
        }
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


def lobby_row(row_id: str = "row-1") -> dict[str, JsonValue]:
    return {
        "__rowId": row_id,
        "host": "host-ku",
        "connected": 3,
        "platform": 1,
        "secondaries": {"1": {"id": "1"}},
    }


def room_row(name: str = "DST cluster") -> dict[str, JsonValue]:
    return {
        "__addr": "127.0.0.1",
        "name": name,
        "port": 10999,
        "connected": 3,
        "maxconnections": 6,
        "password": False,
        "serverpaused": False,
        "season": "autumn",
        "data": "day=12",
        "tick": 12_345,
    }


def rows_payload(rows: list[JsonValue]) -> bytes:
    return jsonlib.dumps({"GET": rows}).encode()


def client(pool: RecordingPool) -> KleiClient:
    return KleiClient(
        access_token=SecretStr("test-token"),
        http_pool=cast("AsyncPoolManager", pool),
    )


async def test_client_reads_only_consumed_version_fields() -> None:
    pool = RecordingPool({VERSION_URL: VERSION_HTML.encode()})

    versions = await client(pool).get_latest_versions()

    versions_by_number = {version.number: version for version in versions}
    assert set(versions_by_number) == {736805, 736959}
    assert versions_by_number[736959].type is VersionType.RELEASE
    assert versions_by_number[736959].date == date(2026, 6, 11)
    assert set(versions_by_number[736959].model_dump()) == {"number", "type", "date"}
    assert pool.calls == [
        {
            "method": "GET",
            "url": VERSION_URL,
            "redirect": True,
        }
    ]


async def test_client_parses_official_lobbies_and_room() -> None:
    region = REGIONS[0]
    lobby_url = LOBBY_URL.format(region=region)
    room_url = ROOM_URL.format(region=region)
    routes = {LOBBY_URL.format(region=item): rows_payload([]) for item in REGIONS}
    pool = RecordingPool(
        routes
        | {
            lobby_url: rows_payload([
                lobby_row() | {"season": "mild", "region": "eu-west-1"},
                {"__rowId": "invalid"},
            ]),
            room_url: rows_payload([room_row()]),
        }
    )

    value = client(pool)
    lobbies = await value.get_lobby_data()
    rooms = await value.get_room_data(((lobbies[0].row_id, region),))

    assert len(lobbies) == 1
    assert lobbies[0].model_dump() == {
        "row_id": "row-1",
        "host": "host-ku",
        "connected": 3,
        "region": region,
    }
    assert len(rooms) == 1
    assert set(rooms[0].model_dump()) == {
        "name",
        "addr",
        "port",
        "connected",
        "maxconnections",
        "password",
        "serverpaused",
        "season",
        "data",
    }
    assert [call["url"] for call in pool.calls[:4]] == [
        LOBBY_URL.format(region=item) for item in REGIONS
    ]
    assert pool.calls[-1]["json"] == {
        "__gameId": "DontStarveTogether",
        "__token": "test-token",
        "query": {"__rowId": "row-1"},
    }
    assert pool.calls[-1]["redirect"] is False


async def test_room_lookup_bounds_shared_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_size = 2
    monkeypatch.setattr(client_module, "_ROOM_CONCURRENCY", batch_size)
    value = client(RecordingPool({}))
    release = Event()
    requests_started = Event()
    active_requests = 0
    max_active_requests = 0

    async def request(*_args: object, **_kwargs: object) -> bytes:
        nonlocal active_requests, max_active_requests
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        if active_requests == batch_size:
            requests_started.set()
        try:
            await release.wait()
        finally:
            active_requests -= 1
        return rows_payload([])

    monkeypatch.setattr(value, "_request", request)
    room_groups = (
        ((f"{group}-{index}", "us-east-1") for index in range(batch_size + 1))
        for group in range(2)
    )
    lookups = tuple(create_task(value.get_room_data(rooms)) for rooms in room_groups)
    try:
        await requests_started.wait()
        await sleep(0)
        assert max_active_requests == batch_size
    finally:
        release.set()
    assert [await lookup for lookup in lookups] == [[], []]


@pytest.mark.parametrize(
    "body",
    [
        rows_payload([room_row() | {"port": "10999"}]),
        b'{"Error":{"Code":"E_FAIL_BUSINESS_LOGIC"}}',
    ],
)
async def test_client_rejects_invalid_room_response(body: bytes) -> None:
    region = "us-east-1"
    pool = RecordingPool({
        ROOM_URL.format(region=region): body,
    })

    with pytest.RaisesGroup(ValidationError):
        await client(pool).get_room_data((("row-1", region),))


async def test_non_success_http_status_consumes_body_before_failing() -> None:
    pool = RecordingPool({VERSION_URL: Reply(VERSION_HTML.encode(), status=500)})

    with pytest.raises(HTTPError, match="HTTP 500"):
        await client(pool).get_latest_versions()

    assert pool.responses[0].body_accessed is True


@pytest.mark.parametrize("stage", ["request", "body"])
async def test_request_has_wall_clock_timeout(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_HTTP_TIMEOUT_SECONDS", 0.01)
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
            await client(pool).get_latest_versions()
    if stage == "body":
        assert pool.responses[0].body_accessed


def test_response_envelope_and_consumed_fields_are_validated() -> None:
    assert KleiDataResponse[LobbyData].model_validate_json("{}").rows == []

    internal = LobbyData.model_validate_json(
        jsonlib.dumps(lobby_row() | {"platform": True}),
        context={"region": "us-east-1"},
    )
    assert internal.model_dump() == {
        "row_id": "row-1",
        "host": "host-ku",
        "connected": 3,
        "region": "us-east-1",
    }

    for changes in (
        {"__rowId": ""},
        {"host": 1},
        {"connected": -1},
        {"connected": "3"},
    ):
        with pytest.raises(ValidationError):
            LobbyData.model_validate_json(
                jsonlib.dumps(lobby_row() | changes),
                context={"region": "us-east-1"},
            )

    with pytest.raises(ValidationError):
        LobbyData.model_validate_json(
            jsonlib.dumps(lobby_row()), context={"region": "invalid"}
        )

    for changes in (
        {"name": 1},
        {"__addr": True},
        {"__addr": 2130706433},
        {"port": 0},
        {"port": 65536},
        {"port": "10999"},
        {"maxconnections": -1},
        {"maxconnections": "6"},
        {"connected": 7},
        {"password": 0},
        {"serverpaused": 0},
        {"season": 1},
        {"data": False},
    ):
        with pytest.raises(ValidationError):
            RoomData.model_validate_json(
                jsonlib.dumps(room_row() | changes),
            )
