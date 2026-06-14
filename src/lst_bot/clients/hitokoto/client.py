from __future__ import annotations

from asyncio import TaskGroup
from collections.abc import Iterable
from http import HTTPMethod
from pathlib import Path
from types import TracebackType
from typing import Self

from logbook import Logger
from pydantic import TypeAdapter
from urllib3_future import AsyncPoolManager
from urllib3_future.util import Url, parse_url

from .cache import is_cache_valid, read_cached_hitokoto, write_cache
from .enums import HitokotoType
from .models import (
    Hitokoto,
    HitokotoBundle,
    HitokotoBundleCategory,
    HitokotoBundleSentence,
    HitokotoBundleVersion,
)

HITOKOTO_BUNDLE_CATEGORIES = TypeAdapter(list[HitokotoBundleCategory])
HITOKOTO_BUNDLE_SENTENCES = TypeAdapter(list[HitokotoBundleSentence])

logger = Logger(__name__)


class HitokotoClient:
    def __init__(
        self,
        *,
        url: str = "https://v1.hitokoto.cn/",
        bundle_url: str = "https://sentences-bundle.hitokoto.cn/",
        http_pool: AsyncPoolManager | None = None,
        cache_path: str | Path = Path(".cache/hitokoto.db"),
        download_cache_on_enter: bool = False,
    ) -> None:
        self.url = url
        self.bundle_url = bundle_url
        self.http_pool = http_pool or AsyncPoolManager()
        self.cache_path = Path(cache_path)
        self.download_cache_on_enter = download_cache_on_enter

    async def __aenter__(self) -> Self:
        if self.download_cache_on_enter:
            await self.ensure_cache()
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

    async def get_hitokoto(
        self,
        types: Iterable[HitokotoType] | None = None,
        use_cache: bool = False,
    ) -> Hitokoto:
        type_values = tuple(types or ())
        if use_cache:
            if __debug__:
                logger.debug(
                    "read Hitokoto cache : {cache_path} {types}",
                    cache_path=self.cache_path,
                    types=type_values,
                )
            await self.ensure_cache()
            return await read_cached_hitokoto(self.cache_path, type_values)

        fields = [("c", item.value) for item in type_values] or None
        if __debug__:
            logger.debug(
                "request Hitokoto : {url} {fields}",
                url=self.url,
                fields=fields,
            )
        response = await self.http_pool.request(
            HTTPMethod.GET,
            self.url,
            fields=fields,
        )
        return Hitokoto.model_validate_json(await response.data)

    async def ensure_cache(self) -> None:
        if await is_cache_valid(self.cache_path):
            if __debug__:
                logger.debug(
                    "Hitokoto cache valid: {cache_path}",
                    cache_path=self.cache_path,
                )
            return
        logger.info(
            "Hitokoto cache stale: {cache_path}",
            cache_path=self.cache_path,
        )
        await self.refresh_cache()

    async def refresh_cache(self) -> None:
        logger.info(
            "refresh Hitokoto cache: {cache_path}",
            cache_path=self.cache_path,
        )
        bundle = await self._download_bundle()
        await write_cache(self.cache_path, bundle)
        logger.info(
            "Hitokoto cache refreshed: {cache_path} ({sentence_count} sentences)",
            cache_path=self.cache_path,
            sentence_count=len(bundle.sentences),
        )
        if __debug__:
            logger.trace("Hitokoto cache bundle : {bundle}", bundle=bundle)

    async def _download_bundle(self) -> HitokotoBundle:
        base_url = bundle_base_url(self.bundle_url)
        if __debug__:
            logger.debug("download Hitokoto bundle metadata : {url}", url=base_url)
        version_response = await self.http_pool.request(
            HTTPMethod.GET,
            bundle_file_url(base_url, "version.json"),
        )
        version = HitokotoBundleVersion.model_validate_json(await version_response.data)
        if __debug__:
            logger.debug(
                "download Hitokoto bundle categories : {path}",
                path=version.categories.path,
            )
        categories_response = await self.http_pool.request(
            HTTPMethod.GET,
            bundle_file_url(base_url, version.categories.path),
        )
        categories = HITOKOTO_BUNDLE_CATEGORIES.validate_json(
            await categories_response.data,
        )

        async with TaskGroup() as group:
            sentence_tasks = [
                group.create_task(
                    self._download_bundle_sentences(base_url, meta.path),
                )
                for meta in version.sentences
            ]
        sentences = [sentence for task in sentence_tasks for sentence in task.result()]
        bundle = HitokotoBundle(
            protocol_version=version.protocol_version,
            bundle_version=version.bundle_version,
            categories=categories,
            sentences=sentences,
        )
        logger.info(
            "Hitokoto bundle downloaded: v{bundle_version} ({sentence_count} "
            "sentences)",
            bundle_version=version.bundle_version,
            sentence_count=len(sentences),
        )
        if __debug__:
            logger.trace("Hitokoto bundle : {bundle}", bundle=bundle)

        return bundle

    async def _download_bundle_sentences(
        self,
        base_url: Url,
        path: str,
    ) -> list[HitokotoBundleSentence]:
        if __debug__:
            logger.debug(
                "download Hitokoto bundle sentences : {path}",
                path=path,
            )
        response = await self.http_pool.request(
            HTTPMethod.GET,
            bundle_file_url(base_url, path),
        )
        return HITOKOTO_BUNDLE_SENTENCES.validate_json(await response.data)


def bundle_base_url(url: str) -> Url:
    value = url.strip()
    if not value:
        msg = "hitokoto bundle URL is empty"
        raise RuntimeError(msg)
    parsed = parse_url(value)
    path = f"{(parsed.path or '/').rstrip('/')}/"
    return parsed._replace(
        scheme=parsed.scheme or "https",
        path=path,
        query=None,
        fragment=None,
    )


def bundle_file_url(base_url: Url, path: str) -> str:
    filename = path.removeprefix("./").lstrip("/")
    base_path = base_url.path or "/"
    return base_url._replace(path=f"{base_path}{filename}").url


__all__ = ["HitokotoClient", "bundle_base_url", "bundle_file_url"]
