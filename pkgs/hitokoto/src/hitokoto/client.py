from asyncio import CancelledError, Task, create_task, gather, timeout, wait
from contextlib import aclosing, suppress
from http import HTTPMethod, HTTPStatus
from logging import getLogger
from pathlib import Path
from types import TracebackType
from typing import Annotated, Literal, Self

from httpx2 import AsyncClient, HTTPStatusError, RequestError
from pydantic import BaseModel, Field, TypeAdapter

from .cache import read_cached_hitokoto, write_cache
from .models import Hitokoto

HTTP_TIMEOUT_SECONDS = 120.0
_BUNDLE_URL = "https://sentences-bundle.hitokoto.cn/"
_MAX_HTTP_BODY_BYTES = 16 * 1024 * 1024
_HITOKOTO_SENTENCES = TypeAdapter(list[Hitokoto])
_HITOKOTO_BUNDLE = TypeAdapter(
    Annotated[list[Hitokoto], Field(min_length=1)],
)
logger = getLogger(__name__)


def _consume_exception(task: Task[None]) -> None:
    if not task.cancelled():
        task.exception()


class _BundleSentenceMeta(BaseModel):
    path: Annotated[str, Field(pattern=r"^\./sentences/[a-l]\.json$")]


class _BundleVersion(BaseModel):
    protocol_version: Literal["1.0.0"]
    sentences: Annotated[list[_BundleSentenceMeta], Field(min_length=1, max_length=12)]


class HitokotoClient:
    def __init__(
        self,
        *,
        http_client: AsyncClient,
        cache_path: str | Path = Path(".cache/hitokoto.db"),
    ) -> None:
        self.http_client = http_client
        self.cache_path = Path(cache_path)
        self._refresh_task: Task[None] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        task = self._refresh_task
        if task is None:
            return
        if not task.cancelling():
            task.cancel()
        cancellation = None
        # Finish closing the response before propagating shutdown cancellation.
        while not task.done():
            try:
                await wait((task,))
            except CancelledError as error:
                cancellation = error
        self._refresh_task = None
        if cancellation is not None:
            raise cancellation

    async def get_hitokoto(self) -> Hitokoto:
        if self._refresh_task is not None and self._refresh_task.done():
            self._refresh_task = None
        with suppress(Exception):
            return await read_cached_hitokoto(self.cache_path, fresh=True)
        task = self._refresh_task
        if task is None:
            task = self._refresh_task = create_task(
                self._refresh_cache(),
                name="hitokoto-cache-refresh",
            )
            task.add_done_callback(_consume_exception)
        try:
            await wait((task,))
            await task
            return await read_cached_hitokoto(self.cache_path)
        except Exception:
            with suppress(Exception):
                return await read_cached_hitokoto(self.cache_path)
            raise

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
                        await self._get(f"{_BUNDLE_URL}{item.path.removeprefix('./')}")
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
        async with self.http_client.stream(
            HTTPMethod.GET,
            url,
            follow_redirects=False,
            timeout=HTTP_TIMEOUT_SECONDS,
        ) as response:

            async def read() -> bytes:
                body = bytearray()
                async with aclosing(
                    response.aiter_bytes(chunk_size=64 * 1024)
                ) as chunks:
                    async for chunk in chunks:
                        if len(body) + len(chunk) > _MAX_HTTP_BODY_BYTES:
                            msg = (
                                "Hitokoto response exceeds "
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
            if response.status_code != HTTPStatus.OK:
                msg = f"Hitokoto request failed: HTTP {response.status_code}"
                raise HTTPStatusError(msg, request=response.request, response=response)
            return body
