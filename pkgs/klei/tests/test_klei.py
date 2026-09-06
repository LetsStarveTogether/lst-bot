import gzip
import json as jsonlib
from asyncio import CancelledError, Event, create_task, gather, sleep, timeout
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import date

import klei.client as client_module
import pytest
from httpx2 import (
    AsyncByteStream,
    AsyncClient,
    HTTPError,
    MockTransport,
    Request,
    Response,
)
from klei import (
    KleiClient,
    LobbyData,
    RoomData,
    VersionType,
)
from klei.models import KleiDataResponse, Region
from pydantic import JsonValue, SecretStr, ValidationError

VERSION_URL = "https://kleiforums.com/game-updates/dst/"
LOBBY_URL = "https://lobby-v2-cdn.klei.com/{region}-Steam.json.gz"
ROOM_URL = "https://lobby-v2-{region}.klei.com/lobby/read"
REGIONS: tuple[Region, ...] = (
    "us-east-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-east-1",
)

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
    headers: Mapping[str, str] | None = None


class BodyStream(AsyncByteStream):
    def __init__(
        self,
        body: bytes,
        body_release: Event | None,
        *,
        release_close: Event | None = None,
    ) -> None:
        self._body = body
        self._body_release = body_release
        self.body_started = Event()
        self.close_started = Event()
        self.release_close = release_close
        self.body_accessed = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.body_accessed = True
        self.body_started.set()
        if self._body_release is not None:
            await self._body_release.wait()
        yield self._body

    async def aclose(self) -> None:
        self.close_started.set()
        if self.release_close is not None:
            await self.release_close.wait()
        self.closed = True


class RecordingTransport(MockTransport):
    def __init__(self, routes: Mapping[str, bytes | Reply]) -> None:
        super().__init__(self.respond)
        self.routes = routes
        self.calls: list[Request] = []
        self.responses: list[Response] = []
        self.streams: list[BodyStream] = []

    async def respond(self, request: Request) -> Response:
        self.calls.append(request)
        result = self.routes[str(request.url)]
        reply = result if isinstance(result, Reply) else Reply(result)
        if reply.release is not None:
            await reply.release.wait()
        stream = BodyStream(reply.body, reply.body_release)
        response = Response(reply.status, headers=reply.headers, stream=stream)
        self.streams.append(stream)
        self.responses.append(response)
        return response


def lobby_row() -> dict[str, JsonValue]:
    return {
        "__rowId": "row-1",
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


def client(transport: RecordingTransport) -> KleiClient:
    return KleiClient(
        access_token=SecretStr("test-token"),
        http_client=AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=True,
        ),
    )


async def test_client_reads_only_consumed_version_fields() -> None:
    transport = RecordingTransport({VERSION_URL: VERSION_HTML.encode()})

    versions = await client(transport).get_latest_versions()

    versions_by_number = {version.number: version for version in versions}
    assert set(versions_by_number) == {736805, 736959}
    assert versions_by_number[736959].type is VersionType.RELEASE
    assert versions_by_number[736959].date == date(2026, 6, 11)
    assert set(versions_by_number[736959].model_dump()) == {"number", "type", "date"}
    (request,) = transport.calls
    assert request.method == "GET"
    assert str(request.url) == VERSION_URL
    assert request.extensions["timeout"] == {
        "connect": 30.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 30.0,
    }


async def test_client_parses_official_lobbies_and_room() -> None:
    region = REGIONS[0]
    lobby_url = LOBBY_URL.format(region=region)
    room_url = ROOM_URL.format(region=region)
    routes = {LOBBY_URL.format(region=item): rows_payload([]) for item in REGIONS}
    transport = RecordingTransport(
        routes
        | {
            lobby_url: rows_payload([
                lobby_row() | {"season": "mild", "region": "eu-west-1"},
                {"__rowId": "invalid"},
            ]),
            room_url: rows_payload([room_row() | {"port": 0}]),
        }
    )

    value = client(transport)
    (lobby,) = await value.get_lobby_data()
    (room,) = await value.get_room_data(((lobby.row_id, region),))

    assert lobby.model_dump() == {
        "row_id": "row-1",
        "host": "host-ku",
        "connected": 3,
        "region": region,
    }
    assert set(room.model_dump()) == {
        "name",
        "connected",
        "maxconnections",
        "password",
        "serverpaused",
        "season",
        "data",
    }
    assert [str(call.url) for call in transport.calls[:4]] == [
        LOBBY_URL.format(region=item) for item in REGIONS
    ]
    assert transport.calls[-1].method == "POST"
    assert jsonlib.loads(transport.calls[-1].content) == {
        "__gameId": "DontStarveTogether",
        "__token": "test-token",
        "query": {"__rowId": "row-1"},
    }


async def test_room_lookup_bounds_shared_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_size = 2
    monkeypatch.setattr(client_module, "_ROOM_CONCURRENCY", batch_size)
    value = client(RecordingTransport({}))
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
    async with timeout(1):
        try:
            await requests_started.wait()
            await sleep(0)
            assert max_active_requests == batch_size
        finally:
            release.set()
        assert [await lookup for lookup in lookups] == [[], []]


async def test_room_lookup_streams_unique_refs_in_first_seen_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_ROOM_CONCURRENCY", 2)
    east, europe = REGIONS[:2]
    transport = RecordingTransport({
        ROOM_URL.format(region=east): rows_payload([room_row("east")]),
        ROOM_URL.format(region=europe): rows_payload([room_row("europe")]),
    })

    def rooms() -> Iterator[tuple[str, Region]]:
        yield "row-1", east
        yield "row-1", east
        assert len(transport.calls) == 1
        yield "row-1", europe
        yield "row-1", east

    results = await client(transport).get_room_data(rooms())

    assert [result.name for result in results] == ["east", "europe"]
    assert [str(call.url) for call in transport.calls] == [
        ROOM_URL.format(region=east),
        ROOM_URL.format(region=europe),
    ]


async def test_room_lookup_has_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_ROOM_CONCURRENCY", 1)
    monkeypatch.setattr(client_module, "_HTTP_TIMEOUT_SECONDS", 0.01)
    value = client(RecordingTransport({}))
    calls = 0

    async def request(*_args: object, **_kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            await Event().wait()
        return rows_payload([])

    monkeypatch.setattr(value, "_request", request)
    with pytest.raises(TimeoutError):
        await value.get_room_data((("row-1", REGIONS[0]), ("row-2", REGIONS[0])))
    assert calls == 2


@pytest.mark.parametrize(
    "body",
    [
        rows_payload([room_row() | {"connected": "3"}]),
        rows_payload([room_row(), room_row("duplicate")]),
        b'{"Error":{"Code":"E_FAIL_BUSINESS_LOGIC"}}',
    ],
)
async def test_client_rejects_invalid_room_response(body: bytes) -> None:
    region = "us-east-1"
    transport = RecordingTransport({
        ROOM_URL.format(region=region): body,
    })

    with pytest.RaisesGroup(ValidationError):
        await client(transport).get_room_data((("row-1", region),))


@pytest.mark.parametrize("body", [b"{}", b'{"Error":{"Code":"E_FAIL"}}'])
async def test_client_rejects_invalid_lobby_envelope(body: bytes) -> None:
    routes = {LOBBY_URL.format(region=region): rows_payload([]) for region in REGIONS}
    routes[LOBBY_URL.format(region=REGIONS[0])] = body

    with pytest.RaisesGroup(ValidationError):
        await client(RecordingTransport(routes)).get_lobby_data()


async def test_non_success_http_status_consumes_body_before_failing() -> None:
    transport = RecordingTransport({
        VERSION_URL: Reply(VERSION_HTML.encode(), status=500)
    })

    with pytest.raises(HTTPError, match="500 Internal Server Error"):
        await client(transport).get_latest_versions()

    assert transport.streams[0].body_accessed
    assert transport.streams[0].closed
    assert transport.responses[0].is_closed


async def test_client_does_not_follow_redirects() -> None:
    transport = RecordingTransport({
        VERSION_URL: Reply(
            b"redirect", status=302, headers={"location": "https://example.com/"}
        )
    })

    with pytest.raises(HTTPError, match="302 Found"):
        await client(transport).get_latest_versions()

    assert [str(call.url) for call in transport.calls] == [VERSION_URL]
    assert transport.streams[0].body_accessed
    assert transport.streams[0].closed


@pytest.mark.parametrize("compressed", [False, True])
async def test_response_body_boundary_closes_connection(
    compressed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VERSION_HTML.encode()
    monkeypatch.setattr(client_module, "_MAX_HTTP_BODY_BYTES", len(body))
    headers = {"content-encoding": "gzip"} if compressed else {}
    accepted = RecordingTransport({
        VERSION_URL: Reply(gzip.compress(body) if compressed else body, headers=headers)
    })
    rejected_body = body + b"x"
    rejected = RecordingTransport({
        VERSION_URL: Reply(
            gzip.compress(rejected_body) if compressed else rejected_body,
            headers=headers,
        )
    })

    await client(accepted).get_latest_versions()
    with pytest.raises(HTTPError, match="response body exceeds"):
        await client(rejected).get_latest_versions()

    for transport in (accepted, rejected):
        assert transport.streams[0].body_accessed
        assert transport.streams[0].closed
        assert transport.responses[0].is_closed


@pytest.mark.parametrize("stage", ["request", "body"])
async def test_request_has_wall_clock_timeout(
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_HTTP_TIMEOUT_SECONDS", 0.01)
    blocked = Event()
    transport = RecordingTransport({
        VERSION_URL: Reply(
            VERSION_HTML.encode(),
            release=blocked if stage == "request" else None,
            body_release=blocked if stage == "body" else None,
        )
    })

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await client(transport).get_latest_versions()
    if stage == "body":
        assert transport.streams[0].body_accessed
        assert transport.streams[0].closed
        assert transport.responses[0].is_closed


@pytest.mark.parametrize("stage", ["body", "close"])
async def test_repeated_cancellation_waits_for_response_close(stage: str) -> None:
    release_body = Event()
    release_close = Event()
    stream = BodyStream(
        VERSION_HTML.encode(),
        release_body if stage == "body" else None,
        release_close=release_close,
    )
    async with AsyncClient(
        transport=MockTransport(lambda _: Response(200, stream=stream)),
        trust_env=False,
    ) as http_client:
        value = KleiClient(SecretStr("test-token"), http_client=http_client)
        task = create_task(value.get_latest_versions())
        try:
            async with timeout(1):
                started = (
                    stream.body_started if stage == "body" else stream.close_started
                )
                await started.wait()
                task.cancel()
                await stream.close_started.wait()
                for _ in range(2):
                    task.cancel()
                    await sleep(0)
                    assert not task.done()
                release_close.set()
                with pytest.raises(CancelledError):
                    await task
                assert stream.closed
        finally:
            release_body.set()
            release_close.set()
            task.cancel()
            await gather(task, return_exceptions=True)


def test_response_envelope_and_consumed_fields_are_validated() -> None:
    assert KleiDataResponse[LobbyData].model_validate_json('{"GET":[]}').rows == []

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
