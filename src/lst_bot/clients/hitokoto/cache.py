from __future__ import annotations

from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import apsw

from .enums import HitokotoType
from .models import Hitokoto, HitokotoBundle

CACHE_MAX_AGE = timedelta(hours=72)


async def is_cache_valid(cache_path: Path) -> bool:
    try:
        async with aclosing(
            await apsw.Connection.as_async(
                str(cache_path),
                flags=apsw.SQLITE_OPEN_READONLY,
            ),
        ) as db:
            cursor = await db.execute("SELECT updated_at FROM version")
            row = await cursor.fetchone()
    except apsw.Error:
        return False

    if row is None:
        return False

    try:
        updated_at = datetime.fromisoformat(row[0])
    except TypeError, ValueError:
        return False

    return datetime.now(UTC) - updated_at <= CACHE_MAX_AGE


async def write_cache(cache_path: Path, bundle: HitokotoBundle) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    try:
        async with aclosing(await apsw.Connection.as_async(str(temp_path))) as db, db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                "CREATE TABLE version ("
                "protocol_version TEXT NOT NULL,"
                "bundle_version TEXT NOT NULL,"
                "updated_at TEXT NOT NULL"
                ");"
                "CREATE TABLE category ("
                "id INTEGER PRIMARY KEY,"
                "key TEXT NOT NULL UNIQUE,"
                "name TEXT NOT NULL,"
                "description TEXT NOT NULL,"
                "path TEXT NOT NULL,"
                "created_at TEXT NOT NULL,"
                "updated_at TEXT NOT NULL"
                ");"
                "CREATE TABLE sentence ("
                "id INTEGER PRIMARY KEY,"
                "uuid TEXT NOT NULL UNIQUE,"
                "hitokoto TEXT NOT NULL,"
                "type TEXT NOT NULL REFERENCES category(key),"
                "source TEXT NOT NULL,"
                "from_who TEXT,"
                "creator TEXT NOT NULL,"
                "creator_uid INTEGER NOT NULL,"
                "reviewer INTEGER NOT NULL,"
                "commit_from TEXT NOT NULL,"
                "created_at TEXT NOT NULL,"
                "length INTEGER NOT NULL"
                ");"
                "CREATE INDEX idx_sentence_type ON sentence(type);",
            )
            await db.execute(
                "INSERT INTO version ("
                "protocol_version,"
                "bundle_version,"
                "updated_at"
                ") VALUES (?, ?, ?)",
                (
                    bundle.protocol_version,
                    bundle.bundle_version,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await db.executemany(
                "INSERT INTO category ("
                "id,"
                "key,"
                "name,"
                "description,"
                "path,"
                "created_at,"
                "updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        item.id,
                        item.key.value,
                        item.name,
                        item.desc,
                        item.path,
                        item.created_at.isoformat(),
                        item.updated_at.isoformat(),
                    )
                    for item in bundle.categories
                ),
            )
            await db.executemany(
                "INSERT INTO sentence ("
                "id,"
                "uuid,"
                "hitokoto,"
                "type,"
                "source,"
                "from_who,"
                "creator,"
                "creator_uid,"
                "reviewer,"
                "commit_from,"
                "created_at,"
                "length"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        item.length,
                    )
                    for item in bundle.sentences
                ),
            )
        temp_path.replace(cache_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def read_cached_hitokoto(
    cache_path: Path,
    types: tuple[HitokotoType, ...],
) -> Hitokoto:
    type_values = tuple(item.value for item in types)
    query = (
        "SELECT "
        "id,"
        "uuid,"
        "hitokoto,"
        "type,"
        "source AS [from],"
        "from_who,"
        "creator,"
        "creator_uid,"
        "reviewer,"
        "commit_from,"
        "created_at "
        "FROM sentence "
        "ORDER BY RANDOM() "
        "LIMIT 1"
    )
    params: tuple[str, ...] = ()
    if type_values:
        placeholders = ", ".join("?" for _ in type_values)
        query = (
            "SELECT "  # noqa: S608
            "id,"
            "uuid,"
            "hitokoto,"
            "type,"
            "source AS [from],"
            "from_who,"
            "creator,"
            "creator_uid,"
            "reviewer,"
            "commit_from,"
            "created_at "
            "FROM sentence "
            f"WHERE type IN ({placeholders}) "
            "ORDER BY RANDOM() "
            "LIMIT 1"
        )
        params = type_values

    async with aclosing(
        await apsw.Connection.as_async(
            str(cache_path),
            flags=apsw.SQLITE_OPEN_READONLY,
        ),
    ) as db:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        if row is not None:
            columns = (column[0] for column in cursor.description)
            payload = dict(zip(columns, row, strict=True))
            payload["created_at"] = datetime.fromisoformat(
                str(payload["created_at"]),
            )

    if row is None:
        msg = "hitokoto cache has no matching sentences"
        raise RuntimeError(msg)
    return Hitokoto.model_validate(payload)


__all__ = ["is_cache_valid", "read_cached_hitokoto", "write_cache"]
