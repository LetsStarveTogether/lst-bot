from asyncio import Task, create_task, shield, timeout
from contextlib import suppress
from http import HTTPMethod, HTTPStatus
from logging import getLogger
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter
from urllib3_future import AsyncPoolManager
from urllib3_future.exceptions import HTTPError

from .cache import read_cached_hitokoto, write_cache
from .models import Hitokoto

HTTP_TIMEOUT_SECONDS = 30.0
_BUNDLE_URL = "https://sentences-bundle.hitokoto.cn/"
_MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024
_HITOKOTO_SENTENCES = TypeAdapter(list[Hitokoto])
_HITOKOTO_BUNDLE = TypeAdapter(
    Annotated[list[Hitokoto], Field(min_length=1)],
)
logger = getLogger(__name__)


class _BundleSentenceMeta(BaseModel):
    path: Annotated[str, Field(pattern=r"^\./sentences/[a-l]\.json$")]


class _BundleVersion(BaseModel):
    protocol_version: Literal["1.0.0"]
    sentences: Annotated[list[_BundleSentenceMeta], Field(min_length=1, max_length=12)]


class HitokotoClient:
    def __init__(
        self,
        *,
        http_pool: AsyncPoolManager,
        cache_path: str | Path = Path(".cache/hitokoto.db"),
    ) -> None:
        self.http_pool = http_pool
        self.cache_path = Path(cache_path)
        self._refresh_task: Task[None] | None = None

    async def get_hitokoto(self) -> Hitokoto:
        if self._refresh_task is not None and self._refresh_task.done():
            self._refresh_task = None
        with suppress(Exception):
            return await read_cached_hitokoto(self.cache_path, fresh=True)
        try:
            task = self._refresh_task
            if task is None:
                task = self._refresh_task = create_task(
                    self._refresh_cache(),
                    name="hitokoto-cache-refresh",
                )
            await shield(task)
            return await read_cached_hitokoto(self.cache_path)
        except Exception as error:
            try:
                return await read_cached_hitokoto(self.cache_path)
            except Exception:
                raise error from None

    async def _refresh_cache(self) -> None:
        logger.info("refresh Hitokoto cache: %s", self.cache_path)
        try:
            async with timeout(HTTP_TIMEOUT_SECONDS):
                version = _BundleVersion.model_validate_json(
                    await self._get(f"{_BUNDLE_URL}version.json"),
                )
                sentences = _HITOKOTO_BUNDLE.validate_python([
                    sentence
                    for item in version.sentences
                    for sentence in _HITOKOTO_SENTENCES.validate_json(
                        await self._get(
                            f"{_BUNDLE_URL}{item.path.removeprefix('./').lstrip('/')}"
                        )
                    )
                ])
                await write_cache(self.cache_path, sentences)
        except Exception:
            logger.exception(
                "Hitokoto cache refresh failed: %s",
                self.cache_path,
            )
            raise
        logger.info(
            "Hitokoto cache refreshed: %s (%d sentences)",
            self.cache_path,
            len(sentences),
        )

    async def _get(
        self,
        url: str,
    ) -> bytes:
        response = await self.http_pool.request(
            HTTPMethod.GET,
            url,
            preload_content=False,
            redirect=False,
            retries=False,
        )
        try:
            body = await response.read(
                _MAX_HTTP_BODY_BYTES + 1,
                decode_content=True,
            )
        finally:
            await response.close()
        if len(body) > _MAX_HTTP_BODY_BYTES:
            msg = f"Hitokoto response exceeds {_MAX_HTTP_BODY_BYTES} bytes"
            raise HTTPError(msg)
        if response.status != HTTPStatus.OK:
            msg = f"Hitokoto request failed: HTTP {response.status}"
            raise HTTPError(msg)
        return body
