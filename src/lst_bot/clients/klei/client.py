from __future__ import annotations

from asyncio import Semaphore, TaskGroup
from collections.abc import Iterable
from http import HTTPMethod
from itertools import chain, product
from types import TracebackType
from typing import Self

from logbook import Logger
from pydantic import OnErrorOmit, SecretStr
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from .enums import Platform, Region
from .models import (
    BuildVersions,
    KleiDataResponse,
    LobbyData,
    RegionCapabilities,
    RoomData,
    Version,
    VersionPage,
)

logger = Logger(__name__)


class KleiClient:
    def __init__(
        self,
        access_token: SecretStr,
        *,
        build_url: str = "https://s3.amazonaws.com/dstbuilds/builds.json",
        version_url: str = "https://forums.kleientertainment.com/game-updates/dst/",
        region_url: str = "https://lobby-v2-cdn.klei.com/regioncapabilities-v2.json",
        lobby_url: str = "https://lobby-v2-cdn.klei.com/{region}-{platform}.json.gz",
        room_url: str = "https://lobby-v2-{region}.klei.com/lobby/read",
        lobby_concurrency: int = 8,
        room_concurrency: int = 24,
        http_pool: AsyncPoolManager | None = None,
    ) -> None:

        self.access_token = access_token
        self.build_url = build_url
        self.version_url = version_url
        self.region_url = region_url
        self.lobby_url = lobby_url
        self.room_url = room_url
        self.lobby_concurrency = lobby_concurrency
        self.room_concurrency = room_concurrency
        self.http_pool = http_pool or AsyncPoolManager()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, exc_tb
        await self.close()

    async def close(self) -> None:
        await self.http_pool.clear()

    async def get_latest_version_number(self, version_type: str = "release") -> int:
        if __debug__:
            logger.debug(
                "request Klei latest version numbers : {url} {version_type}",
                url=self.build_url,
                version_type=version_type,
            )
        response = await self.http_pool.request(HTTPMethod.GET, self.build_url)
        versions = BuildVersions.model_validate_json(await response.data).root[
            version_type
        ]
        latest = max(int(version) for version in versions)
        logger.info(
            "Klei latest {version_type}: {version}",
            version_type=version_type,
            version=latest,
        )
        return latest

    async def get_latest_versions(self) -> list[Version]:
        if __debug__:
            logger.debug("request Klei version page : {url}", url=self.version_url)
        response = await self.http_pool.request(HTTPMethod.GET, self.version_url)
        body = await response.data
        versions = VersionPage.model_validate(body.decode("utf-8")).versions
        logger.info("Klei versions loaded: {count} rows", count=len(versions))
        if __debug__:
            logger.trace("Klei versions : {versions}", versions=versions)
        return versions

    async def get_version_page(self) -> VersionPage:
        if __debug__:
            logger.debug("request Klei version page : {url}", url=self.version_url)
        response = await self.http_pool.request(HTTPMethod.GET, self.version_url)
        body = await response.data
        page = VersionPage.model_validate(body.decode("utf-8"))
        logger.info(
            "Klei version page loaded: {page}/{page_count} ({count} rows)",
            page=page.page,
            page_count=page.page_count,
            count=len(page.versions),
        )
        if __debug__:
            logger.trace("Klei version page : {page}", page=page)
        return page

    async def get_regions(self) -> list[str]:
        if __debug__:
            logger.debug(
                "request Klei region capabilities : {url}", url=self.region_url
            )
        response = await self.http_pool.request(HTTPMethod.GET, self.region_url)
        data = RegionCapabilities.model_validate_json(await response.data)
        regions = [region.region for region in data.lobby_regions]
        logger.info("Klei lobby regions loaded: {count}", count=len(regions))
        if __debug__:
            logger.trace("Klei lobby regions : {regions}", regions=regions)
        return regions

    async def get_lobby_data(
        self,
        regions: Iterable[Region] = Region,
        platforms: Iterable[Platform] = Platform,
    ) -> list[LobbyData]:
        region_values = tuple(regions)
        platform_values = tuple(platforms)
        logger.info(
            "load Klei lobbies: {region_count}x{platform_count}",
            region_count=len(region_values),
            platform_count=len(platform_values),
        )
        if __debug__:
            logger.debug(
                "Klei lobby query: regions={regions} platforms={platforms}",
                regions=", ".join(
                    str(getattr(region, "value", region)) for region in region_values
                )
                or "-",
                platforms=", ".join(
                    str(getattr(platform, "value", platform))
                    for platform in platform_values
                )
                or "-",
            )
        sem = Semaphore(self.lobby_concurrency)
        tasks = set()
        async with TaskGroup() as tg:
            tasks.update(
                tg.create_task(self._get_single_lobby(region, platform, sem))
                for region, platform in product(region_values, platform_values)
            )
        lobbies = list(chain.from_iterable(task.result() for task in tasks))
        logger.info("Klei lobbies loaded: {count} rows", count=len(lobbies))
        if __debug__:
            logger.trace("Klei lobbies : {lobbies}", lobbies=lobbies)
        return lobbies

    async def get_room_data(
        self,
        rooms: Iterable[tuple[str, Region]] | None = None,
    ) -> list[RoomData]:
        if rooms is None:
            lobby_data_list = await self.get_lobby_data()
            rooms = ((data.row_id, data.region) for data in lobby_data_list)

        room_values = tuple(rooms)
        logger.info(
            "load Klei rooms: {count}",
            count=len(room_values),
        )
        if __debug__:
            logger.debug("Klei room query: count={count}", count=len(room_values))
        sem = Semaphore(self.room_concurrency)
        tasks = set()
        async with TaskGroup() as tg:
            tasks.update(
                tg.create_task(self._get_single_room(*room, sem))
                for room in room_values
            )
        room_data = [result for task in tasks if (result := task.result()) is not None]
        logger.info("Klei rooms loaded: {count} rows", count=len(room_data))
        if __debug__:
            logger.trace("Klei rooms : {rooms}", rooms=room_data)
        return room_data

    async def _get_single_lobby(
        self,
        region: Region,
        platform: Platform,
        semaphore: Semaphore,
    ) -> list[LobbyData]:
        url = self.lobby_url.format(region=region, platform=platform.name)
        data = KleiDataResponse[OnErrorOmit[LobbyData]]()
        async with semaphore:
            try:
                if __debug__:
                    logger.debug(
                        "request Klei lobby data : {region} {platform} {url}",
                        region=region,
                        platform=platform,
                        url=url,
                    )
                response = await self.http_pool.request(HTTPMethod.GET, url)
                data = KleiDataResponse[OnErrorOmit[LobbyData]].model_validate_json(
                    await response.data,
                    context={"region": region},
                )
            except HTTPError as exc:
                logger.exception(
                    "Klei lobby request failed: {region}/{platform} ({error})",
                    region=region,
                    platform=platform,
                    error=f"{type(exc).__name__}: {exc}",
                )

        if __debug__:
            logger.debug(
                "Klei lobby rows loaded: {region}/{platform} ({count})",
                region=region,
                platform=platform,
                count=len(data.rows),
            )
            logger.trace(
                "Klei lobby rows : {rows} {region} {platform}",
                rows=data.rows,
                region=region,
                platform=platform,
            )
        return data.rows

    async def _get_single_room(
        self,
        row_id: str,
        region: Region,
        semaphore: Semaphore,
    ) -> RoomData | None:
        url = self.room_url.format(region=region)
        payload = {
            "__gameId": "DontStarveTogether",
            "__token": self.access_token.get_secret_value(),
            "query": {"__rowId": row_id},
        }
        data = KleiDataResponse[OnErrorOmit[RoomData]]()
        room = f"{region}:{row_id}"
        async with semaphore:
            try:
                if __debug__:
                    logger.debug(
                        "request Klei room data: {room}",
                        room=room,
                    )
                response = await self.http_pool.request(
                    HTTPMethod.POST,
                    url,
                    json=payload,
                )
                data = KleiDataResponse[OnErrorOmit[RoomData]].model_validate_json(
                    await response.data,
                    context={"region": region},
                )
            except HTTPError:
                if __debug__:
                    logger.debug(
                        "get Klei room data failed : {room}",
                        room=room,
                    )

        if not data.rows:
            if __debug__:
                logger.debug(
                    "Klei room returned no rows: {room}",
                    room=room,
                )
            return None
        if __debug__:
            logger.debug(
                "Klei room loaded: {room}",
                room=room,
            )
            logger.trace(
                "Klei room data : {room_data} {room}",
                room_data=data.rows[0],
                room=room,
            )
        return data.rows[0]


__all__ = [
    "KleiClient",
]
