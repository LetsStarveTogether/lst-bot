from __future__ import annotations

import sqlite3
from asyncio import to_thread
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time

from .enums import HitokotoType
from .models import Hitokoto


def _open_read_only(cache_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{cache_path.resolve().as_uri()}?mode=ro", uri=True)


def _is_cache_valid(cache_path: Path) -> bool:
    try:
        if not 0 <= time() - cache_path.stat().st_mtime <= 72 * 60 * 60:
            return False
        with closing(_open_read_only(cache_path)) as db:
            return db.execute("SELECT 1 FROM sentence LIMIT 1").fetchone() is not None
    except OSError, sqlite3.Error:
        return False


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
                "hitokoto TEXT NOT NULL,"
                "type TEXT NOT NULL,"
                "source TEXT NOT NULL,"
                "from_who TEXT,"
                "creator TEXT NOT NULL,"
                "creator_uid INTEGER NOT NULL,"
                "reviewer INTEGER NOT NULL,"
                "commit_from TEXT NOT NULL,"
                "created_at TEXT NOT NULL"
                ");"
                "CREATE INDEX idx_sentence_type ON sentence(type);",
            )
            db.executemany(
                "INSERT INTO sentence ("
                "id, uuid, hitokoto, type, source, from_who, creator, creator_uid, "
                "reviewer, commit_from, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        item.id,
                        str(item.uuid),
                        item.hitokoto,
                        item.type.value,
                        item.from_,
                        item.from_who,
                        item.creator,
                        item.creator_uid,
                        item.reviewer,
                        item.commit_from,
                        item.created_at.isoformat(),
                    )
                    for item in sentences
                ),
            )
        temp_path.replace(cache_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def write_cache(cache_path: Path, sentences: Sequence[Hitokoto]) -> None:
    await to_thread(_write_cache, cache_path, sentences)


def _read_cached_hitokoto(
    cache_path: Path,
    types: tuple[HitokotoType, ...],
) -> Hitokoto:
    query = (
        "SELECT id, uuid, hitokoto, type, source AS [from], from_who, creator, "
        "creator_uid, reviewer, commit_from, created_at FROM sentence"
    )
    params = tuple(item.value for item in types)
    placeholders = ", ".join("?" for _ in params)
    query += f" WHERE type IN ({placeholders})" if params else ""
    query += " ORDER BY RANDOM() LIMIT 1"

    with closing(_open_read_only(cache_path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(query, params).fetchone()
    if row is None:
        msg = "hitokoto cache has no matching sentences"
        raise RuntimeError(msg)
    return Hitokoto.model_validate(dict(row))


async def read_cached_hitokoto(
    cache_path: Path,
    types: tuple[HitokotoType, ...],
) -> Hitokoto:
    return await to_thread(_read_cached_hitokoto, cache_path, types)


__all__ = ["is_cache_valid", "read_cached_hitokoto", "write_cache"]
