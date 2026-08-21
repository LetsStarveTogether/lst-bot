from asyncio import Semaphore, TaskGroup, timeout
from collections.abc import Iterable
from http import HTTPMethod, HTTPStatus
from itertools import product
from typing import Annotated, Self

from logbook import Logger
from pydantic import ConfigDict, Field, OnErrorOmit, SecretStr, TypeAdapter
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from .enums import Platform, Region
from .models import (
    KleiDataResponse,
    LobbyData,
    RoomData,
    Version,
    _parse_versions,
)

logger = Logger(__name__)
_DEFAULT_REGIONS: tuple[Region, ...] = (
    "us-east-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-east-1",
)
_POSITIVE_INT = TypeAdapter(Annotated[int, Field(strict=True, gt=0)])
_POSITIVE_FLOAT = TypeAdapter(
    Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
)
_REGIONS = TypeAdapter(tuple[Region, ...])
_PLATFORMS = TypeAdapter(tuple[Platform, ...], config=ConfigDict(strict=True))
_ROOMS = TypeAdapter(
    tuple[tuple[Annotated[str, Field(strict=True, min_length=1)], Region], ...]
)


class KleiClient:
    def __init__(
        self,
        access_token: SecretStr,
        *,
        version_url: str = "https://forums.kleientertainment.com/game-updates/dst/",
        lobby_url: str = "https://lobby-v2-cdn.klei.com/{region}-{platform}.json.gz",
        room_url: str = "https://lobby-v2-{region}.klei.com/lobby/read",
        lobby_concurrency: int = 8,
        room_concurrency: int = 24,
        http_timeout: float = 30.0,
        http_pool: AsyncPoolManager | None = None,
    ) -> None:
        self.access_token = access_token
        self.version_url = version_url
        self.lobby_url = lobby_url
        self.room_url = room_url
        self.http_timeout = _POSITIVE_FLOAT.validate_python(http_timeout)
        self._owns_http_pool = http_pool is None
        self.http_pool = http_pool if http_pool is not None else AsyncPoolManager()
        self._lobby_slots = Semaphore(_POSITIVE_INT.validate_python(lobby_concurrency))
        self._room_slots = Semaphore(_POSITIVE_INT.validate_python(room_concurrency))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http_pool:
            await self.http_pool.clear()

    async def get_latest_versions(self) -> list[Version]:
        body = await self._request(HTTPMethod.GET, self.version_url)
        versions = _parse_versions(body.decode())
        logger.info("Klei versions loaded: {count} rows", count=len(versions))
        return versions

    async def get_lobby_data(
        self,
        regions: Iterable[Region] = _DEFAULT_REGIONS,
        platforms: Iterable[Platform] = Platform,
    ) -> list[LobbyData]:
        region_values = _REGIONS.validate_python(tuple(regions))
        platform_values = _PLATFORMS.validate_python(tuple(platforms))
        logger.info(
            "load Klei lobbies: {region_count}x{platform_count}",
            region_count=len(region_values),
            platform_count=len(platform_values),
        )
        async with TaskGroup() as tg:
            tasks = [
                tg.create_task(self._get_single_lobby(region, platform))
                for region, platform in product(region_values, platform_values)
            ]
        lobbies = [row for task in tasks for row in task.result()]
        logger.info("Klei lobbies loaded: {count} rows", count=len(lobbies))
        return lobbies

    async def get_room_data(
        self,
        rooms: Iterable[tuple[str, Region]] | None = None,
    ) -> list[RoomData]:
        if rooms is None:
            lobby_data_list = await self.get_lobby_data()
            rooms = ((data.row_id, data.region) for data in lobby_data_list)

        room_values = _ROOMS.validate_python(tuple(rooms))
        logger.info("load Klei rooms: {count}", count=len(room_values))
        async with TaskGroup() as tg:
            tasks = [
                tg.create_task(self._get_single_room(*room)) for room in room_values
            ]
        room_data = [result for task in tasks if (result := task.result()) is not None]
        logger.info("Klei rooms loaded: {count} rows", count=len(room_data))
        return room_data

    async def _get_single_lobby(
        self,
        region: Region,
        platform: Platform,
    ) -> list[LobbyData]:
        url = self.lobby_url.format(region=region, platform=platform.name)
        async with self._lobby_slots:
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
        url = self.room_url.format(region=region)
        payload = {
            "__gameId": "DontStarveTogether",
            "__token": self.access_token.get_secret_value(),
            "query": {"__rowId": row_id},
        }
        async with self._room_slots:
            data = KleiDataResponse[OnErrorOmit[RoomData]].model_validate_json(
                await self._request(HTTPMethod.POST, url, json=payload),
                context={"region": region},
            )
        return data.rows[0] if data.rows else None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: object | None = None,
    ) -> bytes:
        async with timeout(self.http_timeout):
            response = await self.http_pool.request(method, url, json=json)
            if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                msg = f"Klei request failed: HTTP {response.status} {method} {url}"
                raise HTTPError(msg)
            return await response.data
