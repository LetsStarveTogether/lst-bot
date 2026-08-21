from asyncio import Lock, TaskGroup, timeout
from collections.abc import Iterable
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from logbook import Logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from .cache import is_cache_valid, read_cached_hitokoto, write_cache
from .enums import HitokotoType
from .models import Hitokoto

HTTP_TIMEOUT_SECONDS = 30.0
_HITOKOTO_SENTENCES = TypeAdapter(list[Hitokoto])
_HITOKOTO_BUNDLE = TypeAdapter(
    Annotated[list[Hitokoto], Field(min_length=1)],
)
_HITOKOTO_TYPES = TypeAdapter(
    tuple[HitokotoType, ...],
    config=ConfigDict(strict=True),
)
_CACHE_LOCKS: WeakValueDictionary[Path, Lock] = WeakValueDictionary()

logger = Logger(__name__)


class _BundleSentenceMeta(BaseModel):
    path: Annotated[str, Field(min_length=1)]


class _BundleVersion(BaseModel):
    sentences: Annotated[list[_BundleSentenceMeta], Field(min_length=1)]


class HitokotoClient:
    def __init__(
        self,
        *,
        url: str = "https://v1.hitokoto.cn/",
        bundle_url: str = "https://sentences-bundle.hitokoto.cn/",
        http_pool: AsyncPoolManager | None = None,
        cache_path: str | Path = Path(".cache/hitokoto.db"),
    ) -> None:
        self.url = url
        self.bundle_url = bundle_url
        self.http_pool = http_pool if http_pool is not None else AsyncPoolManager()
        self._owns_http_pool = http_pool is None
        self.cache_path = Path(cache_path)
        self._cache_lock = _CACHE_LOCKS.setdefault(self.cache_path.resolve(), Lock())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http_pool:
            await self.http_pool.clear()

    async def get_hitokoto(
        self,
        types: Iterable[HitokotoType] | None = None,
        use_cache: bool = False,
    ) -> Hitokoto:
        type_values = _HITOKOTO_TYPES.validate_python(tuple(types or ()))
        if use_cache:
            await self.ensure_cache()
            return await read_cached_hitokoto(self.cache_path, type_values)

        fields = [("c", item.value) for item in type_values] or None
        return Hitokoto.model_validate_json(await self._get(self.url, fields=fields))

    async def ensure_cache(self) -> None:
        if await is_cache_valid(self.cache_path):
            return
        async with self._cache_lock:
            if await is_cache_valid(self.cache_path):
                return
            logger.info("refresh Hitokoto cache: {path}", path=self.cache_path)
            base_url = _bundle_base_url(self.bundle_url)
            version = _BundleVersion.model_validate_json(
                await self._get(f"{base_url}version.json"),
            )
            async with TaskGroup() as group:
                tasks = [
                    group.create_task(
                        self._get(
                            f"{base_url}{item.path.removeprefix('./').lstrip('/')}",
                        ),
                    )
                    for item in version.sentences
                ]
            sentences = _HITOKOTO_BUNDLE.validate_python([
                sentence
                for task in tasks
                for sentence in _HITOKOTO_SENTENCES.validate_json(task.result())
            ])
            await write_cache(self.cache_path, sentences)
            logger.info(
                "Hitokoto cache refreshed: {path} ({count} sentences)",
                path=self.cache_path,
                count=len(sentences),
            )

    async def _get(
        self,
        url: str,
        *,
        fields: list[tuple[str, str]] | None = None,
    ) -> bytes:
        async with timeout(HTTP_TIMEOUT_SECONDS):
            response = await self.http_pool.request(
                HTTPMethod.GET,
                url,
                fields=fields,
            )
            if response.status != HTTPStatus.OK:
                msg = f"Hitokoto request failed: HTTP {response.status}"
                raise HTTPError(msg)
            return await response.data


def _bundle_base_url(url: str) -> str:
    value = url.strip()
    if not value:
        msg = "hitokoto bundle URL is empty"
        raise RuntimeError(msg)
    parsed = urlsplit(value if "://" in value else f"https://{value.lstrip('/')}")
    return parsed._replace(
        path=f"{parsed.path.rstrip('/')}/",
        query="",
        fragment="",
    ).geturl()


__all__ = ["HitokotoClient"]
