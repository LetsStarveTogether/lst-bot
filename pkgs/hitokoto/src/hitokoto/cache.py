import sqlite3
from asyncio import to_thread
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time

from .models import Hitokoto

_CACHE_MAX_AGE_SECONDS = 72 * 60 * 60


def _open_read_only(cache_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{cache_path.resolve().as_uri()}?mode=ro", uri=True)


def _write_cache(cache_path: Path, sentences: Sequence[Hitokoto]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        prefix=f".{cache_path.name}.",
        suffix=".tmp",
        dir=cache_path.parent,
        delete=False,
    ) as temp:
        temp_path = Path(temp.name)
    try:
        with closing(sqlite3.connect(temp_path)) as db, db:
            db.execute("CREATE TABLE sentence (payload TEXT NOT NULL)")
            db.executemany(
                "INSERT INTO sentence (payload) VALUES (?)",
                ((item.model_dump_json(by_alias=True),) for item in sentences),
            )
        temp_path.replace(cache_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def write_cache(cache_path: Path, sentences: Sequence[Hitokoto]) -> None:
    await to_thread(_write_cache, cache_path, sentences)


def _read_cached_hitokoto(cache_path: Path, *, fresh: bool) -> Hitokoto:
    if fresh and not (
        0 <= time() - cache_path.stat().st_mtime <= _CACHE_MAX_AGE_SECONDS
    ):
        msg = "hitokoto cache is stale"
        raise RuntimeError(msg)
    with closing(_open_read_only(cache_path)) as db:
        # A cache rebuild only inserts rows, so rowids are contiguous.
        row = db.execute(
            "SELECT payload FROM sentence WHERE rowid = "
            "abs(random()) % (SELECT max(rowid) FROM sentence) + 1"
        ).fetchone()
    if row is None:
        msg = "hitokoto cache has no matching sentences"
        raise RuntimeError(msg)
    return Hitokoto.model_validate_json(row[0])


async def read_cached_hitokoto(
    cache_path: Path,
    *,
    fresh: bool = False,
) -> Hitokoto:
    return await to_thread(_read_cached_hitokoto, cache_path, fresh=fresh)
