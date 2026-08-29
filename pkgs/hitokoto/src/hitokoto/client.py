from asyncio import Lock, TaskGroup, timeout
from http import HTTPMethod, HTTPStatus
from logging import getLogger
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from .cache import is_cache_valid, read_cached_hitokoto, write_cache
from .models import Hitokoto

HTTP_TIMEOUT_SECONDS = 30.0
_BUNDLE_URL = "https://sentences-bundle.hitokoto.cn/"
_HITOKOTO_SENTENCES = TypeAdapter(list[Hitokoto])
_HITOKOTO_BUNDLE = TypeAdapter(
    Annotated[list[Hitokoto], Field(min_length=1)],
)
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
        http_pool: AsyncPoolManager,
        cache_path: str | Path = Path(".cache/hitokoto.db"),
    ) -> None:
        self.http_pool = http_pool
        self.cache_path = Path(cache_path)
        self._cache_lock = Lock()

    async def get_hitokoto(self) -> Hitokoto:
        try:
            await self._ensure_cache()
        except Exception as error:
            try:
                hitokoto = await read_cached_hitokoto(self.cache_path)
            except Exception:
                raise error from None
            logger.warning(
                "use stale Hitokoto cache after refresh failure: %s",
                self.cache_path,
                exc_info=True,
            )
            return hitokoto
        return await read_cached_hitokoto(self.cache_path)

    async def _ensure_cache(self) -> None:
        if await is_cache_valid(self.cache_path):
            return
        async with self._cache_lock:
            if await is_cache_valid(self.cache_path):
                return
            logger.info("refresh Hitokoto cache: %s", self.cache_path)
            version = _BundleVersion.model_validate_json(
                await self._get(f"{_BUNDLE_URL}version.json"),
            )
            async with TaskGroup() as group:
                tasks = [
                    group.create_task(
                        self._get(
                            f"{_BUNDLE_URL}{item.path.removeprefix('./').lstrip('/')}",
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
            body = await response.data
            if response.status != HTTPStatus.OK:
                msg = f"Hitokoto request failed: HTTP {response.status}"
                raise HTTPError(msg)
            return body
