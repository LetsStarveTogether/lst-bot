from asyncio import (
    CancelledError,
    Semaphore,
    TaskGroup,
    create_task,
    gather,
    timeout,
    wait,
)
from collections.abc import Iterable
from contextlib import aclosing, suppress
from http import HTTPMethod
from itertools import batched
from typing import Annotated

from httpx2 import AsyncClient, RequestError
from pydantic import (
    Field,
    OnErrorOmit,
    SecretStr,
    TypeAdapter,
)

from .models import (
    KleiDataResponse,
    LobbyData,
    Region,
    RoomData,
    Version,
    _parse_versions,
    _RoomDataResponse,
)

_DEFAULT_REGIONS: tuple[Region, ...] = (
    "us-east-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-east-1",
)
_VERSION_URL = "https://kleiforums.com/game-updates/dst/"
_LOBBY_URL = "https://lobby-v2-cdn.klei.com/{region}-Steam.json.gz"
_ROOM_URL = "https://lobby-v2-{region}.klei.com/lobby/read"
_HTTP_TIMEOUT_SECONDS = 30.0
_MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024
_ROOM_CONCURRENCY = 24  # ponytail: configure only if Klei throttling demands it
_ROOM = TypeAdapter(tuple[Annotated[str, Field(strict=True, min_length=1)], Region])


class KleiClient:
    def __init__(
        self,
        access_token: SecretStr,
        *,
        http_client: AsyncClient,
    ) -> None:
        self.access_token = access_token
        self.http_client = http_client
        self._room_slots = Semaphore(_ROOM_CONCURRENCY)

    async def get_latest_versions(self) -> list[Version]:
        async with timeout(_HTTP_TIMEOUT_SECONDS):
            body = await self._request(HTTPMethod.GET, _VERSION_URL)
        return _parse_versions(body.decode())

    async def get_lobby_data(self) -> list[LobbyData]:
        async with timeout(_HTTP_TIMEOUT_SECONDS), TaskGroup() as tg:
            tasks = [
                tg.create_task(self._get_single_lobby(region))
                for region in _DEFAULT_REGIONS
            ]
        return [row for task in tasks for row in task.result()]

    async def get_room_data(
        self,
        rooms: Iterable[tuple[str, Region]],
    ) -> list[RoomData]:
        results: list[RoomData] = []
        seen: set[tuple[str, Region]] = set()
        async with timeout(_HTTP_TIMEOUT_SECONDS):
            for values in batched(
                map(_ROOM.validate_python, rooms),
                _ROOM_CONCURRENCY,
                strict=False,
            ):
                batch = tuple(
                    room for room in dict.fromkeys(values) if room not in seen
                )
                seen.update(batch)
                async with TaskGroup() as tg:
                    tasks = [
                        tg.create_task(self._get_single_room(*room)) for room in batch
                    ]
                results.extend(
                    result for task in tasks if (result := task.result()) is not None
                )
        return results

    async def _get_single_lobby(
        self,
        region: Region,
    ) -> list[LobbyData]:
        url = _LOBBY_URL.format(region=region)
        data = KleiDataResponse[OnErrorOmit[LobbyData]].model_validate_json(
            await self._request(HTTPMethod.GET, url),
            context={"region": region},
        )
        return data.rows

    async def _get_single_room(
        self,
        row_id: str,
        region: Region,
    ) -> RoomData | None:
        url = _ROOM_URL.format(region=region)
        payload = {
            "__gameId": "DontStarveTogether",
            "__token": self.access_token.get_secret_value(),
            "query": {"__rowId": row_id},
        }
        async with self._room_slots:
            data = _RoomDataResponse.model_validate_json(
                await self._request(HTTPMethod.POST, url, json=payload),
            )
        return data.rows[0] if data.rows else None

    async def _request(
        self,
        method: HTTPMethod,
        url: str,
        *,
        json: object | None = None,
    ) -> bytes:
        async with self.http_client.stream(
            method,
            url,
            json=json,
            follow_redirects=False,
            timeout=_HTTP_TIMEOUT_SECONDS,
        ) as response:

            async def read() -> bytes:
                body = bytearray()
                async with aclosing(
                    response.aiter_bytes(chunk_size=64 * 1024)
                ) as chunks:
                    async for chunk in chunks:
                        if len(body) + len(chunk) > _MAX_HTTP_BODY_BYTES:
                            msg = (
                                "Klei response body exceeds "
                                f"{_MAX_HTTP_BODY_BYTES} bytes"
                            )
                            raise RequestError(msg, request=response.request)
                        body.extend(chunk)
                return bytes(body)

            reading = create_task(read())

            async def cleanup() -> None:
                await gather(reading, return_exceptions=True)
                with suppress(Exception):
                    await response.aclose()

            try:
                await wait((reading,))
                body = reading.result()
            finally:
                # Let an in-flight close finish; cancel the body reader at most once.
                if not reading.done() and not response.is_closed:
                    reading.cancel()
                cleaning = create_task(cleanup())
                cancelled = None
                while not cleaning.done():
                    try:
                        await wait((cleaning,))
                    except CancelledError as error:
                        cancelled = error
                cleaning.result()
                if cancelled is not None:
                    raise cancelled
            response.raise_for_status()
            return body
