import sqlite3
from asyncio import to_thread
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time

from .models import Hitokoto


def _open_read_only(cache_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{cache_path.resolve().as_uri()}?mode=ro", uri=True)


def _is_cache_valid(cache_path: Path) -> bool:
    try:
        if not 0 <= time() - cache_path.stat().st_mtime <= 72 * 60 * 60:
            return False
        with closing(_open_read_only(cache_path)) as db:
            row = db.execute("SELECT payload FROM sentence LIMIT 1").fetchone()
        if row is None:
            return False
        Hitokoto.model_validate_json(row[0])
    except OSError, sqlite3.Error, ValueError:
        return False
    else:
        return True


async def is_cache_valid(cache_path: Path) -> bool:
    return await to_thread(_is_cache_valid, cache_path)


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
            db.executescript(
                "CREATE TABLE sentence ("
                "id INTEGER PRIMARY KEY,"
                "uuid TEXT NOT NULL UNIQUE,"
                "payload TEXT NOT NULL"
                ");",
            )
            db.executemany(
                "INSERT INTO sentence (id, uuid, payload) VALUES (?, ?, ?)",
                (
                    (
                        item.id,
                        str(item.uuid),
                        item.model_dump_json(by_alias=True),
                    )
                    for item in sentences
                ),
            )
        temp_path.replace(cache_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def write_cache(cache_path: Path, sentences: Sequence[Hitokoto]) -> None:
    await to_thread(_write_cache, cache_path, sentences)


def _read_cached_hitokoto(cache_path: Path) -> Hitokoto:
    with closing(_open_read_only(cache_path)) as db:
        row = db.execute(
            "SELECT payload FROM sentence ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    if row is None:
        msg = "hitokoto cache has no matching sentences"
        raise RuntimeError(msg)
    return Hitokoto.model_validate_json(row[0])


async def read_cached_hitokoto(cache_path: Path) -> Hitokoto:
    return await to_thread(_read_cached_hitokoto, cache_path)
