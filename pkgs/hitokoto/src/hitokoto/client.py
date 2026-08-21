from asyncio import Lock, TaskGroup, timeout
from http import HTTPMethod, HTTPStatus
from logging import getLogger
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from pydantic import AnyUrl, BaseModel, Field, TypeAdapter, UrlConstraints
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from .cache import is_cache_valid, read_cached_hitokoto, write_cache
from .models import Hitokoto

HTTP_TIMEOUT_SECONDS = 30.0
_HITOKOTO_SENTENCES = TypeAdapter(list[Hitokoto])
_HITOKOTO_BUNDLE = TypeAdapter(
    Annotated[list[Hitokoto], Field(min_length=1)],
)
_HTTPS_URL = TypeAdapter(
    Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]
)
_CACHE_LOCKS: WeakValueDictionary[Path, Lock] = WeakValueDictionary()
logger = getLogger(__name__)


class _BundleSentenceMeta(BaseModel):
    path: Annotated[str, Field(min_length=1)]


class _BundleVersion(BaseModel):
    protocol_version: Literal["1.0.0"]
    sentences: Annotated[list[_BundleSentenceMeta], Field(min_length=1)]


class HitokotoClient:
    def __init__(
        self,
        *,
        url: str = "https://v1.hitokoto.cn/",
        bundle_url: str = "https://sentences-bundle.hitokoto.cn/",
        http_pool: AsyncPoolManager,
        cache_path: str | Path = Path(".cache/hitokoto.db"),
    ) -> None:
        self.url = str(_HTTPS_URL.validate_python(url))
        self.bundle_url = str(_HTTPS_URL.validate_python(bundle_url))
        self.http_pool = http_pool
        self.cache_path = Path(cache_path)
        self._cache_lock = _CACHE_LOCKS.setdefault(self.cache_path.resolve(), Lock())

    async def get_hitokoto(
        self,
        *,
        use_cache: bool = False,
    ) -> Hitokoto:
        if use_cache:
            await self.ensure_cache()
            return await read_cached_hitokoto(self.cache_path)

        return Hitokoto.model_validate_json(await self._get(self.url))

    async def ensure_cache(self) -> None:
        if await is_cache_valid(self.cache_path):
            return
        async with self._cache_lock:
            if await is_cache_valid(self.cache_path):
                return
            logger.info("refresh Hitokoto cache: %s", self.cache_path)
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
                "Hitokoto cache refreshed: %s (%d sentences)",
                self.cache_path,
                len(sentences),
            )

    async def _get(
        self,
        url: str,
    ) -> bytes:
        async with timeout(HTTP_TIMEOUT_SECONDS):
            response = await self.http_pool.request(
                HTTPMethod.GET,
                url,
            )
            if response.status != HTTPStatus.OK:
                msg = f"Hitokoto request failed: HTTP {response.status}"
                raise HTTPError(msg)
            return await response.data


def _bundle_base_url(url: str) -> str:
    parsed = urlsplit(url)
    return parsed._replace(
        path=f"{parsed.path.rstrip('/')}/",
        query="",
        fragment="",
    ).geturl()
