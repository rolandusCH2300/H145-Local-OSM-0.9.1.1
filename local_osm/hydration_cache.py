from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .db import (
    decode_way_geometry_node_ids,
    decode_way_node_ids,
    encode_way_node_ids,
    get_way_node_ids_by_way,
    sqlite_in_chunk_size,
)

PACKED_TAGS_VERSION = 1
PACKED_WAY_IDS_VERSION = 1
PACKED_TAGS_TABLE = "packed_object_tags"
PACKED_WAY_IDS_TABLE = "packed_way_node_ids"


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _meta_int(con: sqlite3.Connection, key: str) -> int | None:
    try:
        row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return None


def packed_tags_ready(con: sqlite3.Connection) -> bool:
    return (
        _table_exists(con, PACKED_TAGS_TABLE)
        and _meta_int(con, "packed_tags_version") == PACKED_TAGS_VERSION
        and _meta_int(con, "packed_tags_complete") == 1
    )


def packed_way_ids_ready(con: sqlite3.Connection) -> bool:
    return (
        _table_exists(con, PACKED_WAY_IDS_TABLE)
        and _meta_int(con, "packed_way_ids_version") == PACKED_WAY_IDS_VERSION
        and _meta_int(con, "packed_way_ids_complete") == 1
    )


def _set_meta(con: sqlite3.Connection, key: str, value: object) -> None:
    con.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(key), str(value)),
    )


def _clear_ready_meta(con: sqlite3.Connection, keys: tuple[str, ...]) -> None:
    con.executemany("DELETE FROM metadata WHERE key=?", ((key,) for key in keys))


def _encode_tags(tags: dict[str, str]) -> bytes:
    # Compact JSON keeps the derived cache self-describing and Unicode-safe.
    return json.dumps(tags, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_tags(blob: bytes | memoryview | str | None) -> dict[str, str]:
    if blob is None:
        return {}
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    value = json.loads(blob)
    if not isinstance(value, dict):
        raise ValueError("invalid packed tag payload")
    # Version 1 payloads are produced from dict[str, str].
    return value


def build_packed_tags(
    con: sqlite3.Connection, *, verbose: bool = True, batch_size: int = 20_000
) -> tuple[int, int, float]:
    """Build optional packed tags for legacy schemas with complete object_tags.

    Schema v6 stores full tags in packed_object_tags; they cannot be rebuilt
    from its selector-only object_tags table. Untagged objects have no cache
    row and hydrate to an empty mapping.
    """
    started = time.perf_counter()
    if verbose:
        print("[packed-tags] building one-row-per-object hydration cache...")

    con.execute(
        f"CREATE TABLE IF NOT EXISTS {PACKED_TAGS_TABLE}("
        "object_pk INTEGER PRIMARY KEY, tags_json BLOB NOT NULL) WITHOUT ROWID"
    )
    _clear_ready_meta(
        con,
        ("packed_tags_version", "packed_tags_complete", "packed_tags_rows", "packed_tags_bytes"),
    )
    con.execute(f"DELETE FROM {PACKED_TAGS_TABLE}")
    con.commit()

    insert_sql = f"INSERT INTO {PACKED_TAGS_TABLE}(object_pk,tags_json) VALUES(?,?)"
    batch: list[tuple[int, sqlite3.Binary]] = []
    current_pk: int | None = None
    current_tags: dict[str, str] = {}
    rows = 0
    payload_bytes = 0

    def flush_object() -> None:
        nonlocal rows, payload_bytes, current_tags, batch
        if current_pk is None:
            return
        payload = _encode_tags(current_tags)
        payload_bytes += len(payload)
        batch.append((current_pk, sqlite3.Binary(payload)))
        rows += 1
        if len(batch) >= batch_size:
            con.executemany(insert_sql, batch)
            con.commit()
            if verbose and rows % 200_000 < batch_size:
                print(f"[packed-tags] objects={rows:,} payload={payload_bytes / (1024**2):,.1f} MiB")
            batch.clear()

    for row in con.execute(
        "SELECT object_pk,key,value FROM object_tags ORDER BY object_pk,key"
    ):
        pk = int(row[0])
        if current_pk is None:
            current_pk = pk
        elif pk != current_pk:
            flush_object()
            current_pk = pk
            current_tags = {}
        current_tags[str(row[1])] = str(row[2])
    flush_object()
    if batch:
        con.executemany(insert_sql, batch)
        con.commit()

    _set_meta(con, "packed_tags_version", PACKED_TAGS_VERSION)
    _set_meta(con, "packed_tags_complete", 1)
    _set_meta(con, "packed_tags_rows", rows)
    _set_meta(con, "packed_tags_bytes", payload_bytes)
    con.commit()
    elapsed = time.perf_counter() - started
    if verbose:
        print(
            f"[packed-tags] complete: rows={rows:,} payload={payload_bytes / (1024**2):,.1f} MiB "
            f"elapsed={elapsed:,.1f}s"
        )
    return rows, payload_bytes, elapsed


def build_packed_way_ids(
    con: sqlite3.Connection, *, verbose: bool = True, batch_size: int = 20_000
) -> tuple[int, int, float]:
    """Build an optional ID-only topology blob beside schema-4 way geometry."""
    started = time.perf_counter()
    if verbose:
        print("[packed-way-ids] building ID-only way topology cache...")

    con.execute(
        f"CREATE TABLE IF NOT EXISTS {PACKED_WAY_IDS_TABLE}("
        "way_id INTEGER PRIMARY KEY, node_ids BLOB NOT NULL) WITHOUT ROWID"
    )
    _clear_ready_meta(
        con,
        ("packed_way_ids_version", "packed_way_ids_complete", "packed_way_ids_rows", "packed_way_ids_bytes"),
    )
    con.execute(f"DELETE FROM {PACKED_WAY_IDS_TABLE}")
    con.commit()

    insert_sql = f"INSERT INTO {PACKED_WAY_IDS_TABLE}(way_id,node_ids) VALUES(?,?)"
    batch: list[tuple[int, sqlite3.Binary]] = []
    rows = 0
    payload_bytes = 0
    has_v5 = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='way_geometry_v5'"
    ).fetchone() is not None and con.execute(
        "SELECT 1 FROM way_geometry_v5 LIMIT 1"
    ).fetchone() is not None

    if has_v5:
        source_rows = con.execute(
            "SELECT way_id,node_ids FROM way_geometry_v5 ORDER BY way_id"
        )
        for row in source_rows:
            way_id = int(row[0])
            payload = bytes(row[1])
            payload_bytes += len(payload)
            batch.append((way_id, sqlite3.Binary(payload)))
            rows += 1
            if len(batch) >= batch_size:
                con.executemany(insert_sql, batch)
                con.commit()
                if verbose and rows % 200_000 < batch_size:
                    print(f"[packed-way-ids] ways={rows:,} payload={payload_bytes / (1024**2):,.1f} MiB")
                batch.clear()
    else:
        for row in con.execute("SELECT way_id,node_data FROM way_geometry ORDER BY way_id"):
            way_id = int(row[0])
            ids = decode_way_geometry_node_ids(row[1])
            payload = encode_way_node_ids(ids)
            payload_bytes += len(payload)
            batch.append((way_id, sqlite3.Binary(payload)))
            rows += 1
            if len(batch) >= batch_size:
                con.executemany(insert_sql, batch)
                con.commit()
                if verbose and rows % 200_000 < batch_size:
                    print(f"[packed-way-ids] ways={rows:,} payload={payload_bytes / (1024**2):,.1f} MiB")
                batch.clear()
    if batch:
        con.executemany(insert_sql, batch)
        con.commit()

    _set_meta(con, "packed_way_ids_version", PACKED_WAY_IDS_VERSION)
    _set_meta(con, "packed_way_ids_complete", 1)
    _set_meta(con, "packed_way_ids_rows", rows)
    _set_meta(con, "packed_way_ids_bytes", payload_bytes)
    con.commit()
    elapsed = time.perf_counter() - started
    if verbose:
        print(
            f"[packed-way-ids] complete: rows={rows:,} payload={payload_bytes / (1024**2):,.1f} MiB "
            f"elapsed={elapsed:,.1f}s"
        )
    return rows, payload_bytes, elapsed


def load_packed_tags_many(
    con: sqlite3.Connection, object_pks: Iterable[int]
) -> tuple[dict[int, dict[str, str]], float, float]:
    pks = list(dict.fromkeys(int(v) for v in object_pks))
    out: dict[int, dict[str, str]] = {pk: {} for pk in pks}
    sql_ms = 0.0
    decode_ms = 0.0
    chunk_size = sqlite_in_chunk_size(con)
    # Sort multi-chunk lookups by the table's primary-key order.
    fetch_pks = sorted(pks) if len(pks) > chunk_size else pks
    for offset in range(0, len(fetch_pks), chunk_size):
        chunk = fetch_pks[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        started = time.perf_counter()
        rows = con.execute(
            f"SELECT object_pk,tags_json FROM {PACKED_TAGS_TABLE} "
            f"WHERE object_pk IN ({placeholders})",
            chunk,
        ).fetchall()
        sql_ms += (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        for row in rows:
            out[row[0]] = _decode_tags(row[1])
        decode_ms += (time.perf_counter() - started) * 1000.0
    return out, sql_ms, decode_ms


def load_packed_way_ids_many(
    con: sqlite3.Connection, way_ids: Iterable[int]
) -> tuple[dict[int, list[int]], float, float]:
    ids = list(dict.fromkeys(int(v) for v in way_ids))
    out: dict[int, list[int]] = {wid: [] for wid in ids}
    sql_ms = 0.0
    decode_ms = 0.0
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        started = time.perf_counter()
        rows = con.execute(
            f"SELECT way_id,node_ids FROM {PACKED_WAY_IDS_TABLE} "
            f"WHERE way_id IN ({placeholders})",
            chunk,
        ).fetchall()
        sql_ms += (time.perf_counter() - started) * 1000.0
        found: set[int] = set()
        started = time.perf_counter()
        for row in rows:
            wid = int(row[0])
            found.add(wid)
            out[wid] = decode_way_node_ids(row[1])
        decode_ms += (time.perf_counter() - started) * 1000.0
        # Missing cache rows fall back to authoritative topology.
        missing = [wid for wid in chunk if wid not in found]
        if missing:
            started = time.perf_counter()
            out.update(get_way_node_ids_by_way(con, missing))
            sql_ms += (time.perf_counter() - started) * 1000.0
    return out, sql_ms, decode_ms


def inspect_hydration_cache(con: sqlite3.Connection) -> dict[str, object]:
    tags_ready = packed_tags_ready(con)
    ways_ready = packed_way_ids_ready(con)
    tags_rows = _meta_int(con, "packed_tags_rows") or 0
    tag_bytes = _meta_int(con, "packed_tags_bytes") or 0
    way_rows = _meta_int(con, "packed_way_ids_rows") or 0
    way_bytes = _meta_int(con, "packed_way_ids_bytes") or 0
    return {
        "packed_tags_ready": tags_ready,
        "packed_tags_version": PACKED_TAGS_VERSION if tags_ready else 0,
        "packed_tags_rows": tags_rows,
        "packed_tags_bytes": tag_bytes,
        "packed_way_ids_ready": ways_ready,
        "packed_way_ids_version": PACKED_WAY_IDS_VERSION if ways_ready else 0,
        "packed_way_ids_rows": way_rows,
        "packed_way_ids_bytes": way_bytes,
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Build/check response-hydration caches. Tag rebuilding requires legacy "
            "schemas with complete object_tags; schema v6 packed tags require "
            "re-importing OSM."
        )
    )
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--check", action="store_true")
    p.add_argument("--tags-only", action="store_true")
    p.add_argument("--way-ids-only", action="store_true")
    args = p.parse_args()
    if args.tags_only and args.way_ids_only:
        p.error("--tags-only and --way-ids-only are mutually exclusive")

    db = args.db.resolve()
    if not db.is_file():
        raise SystemExit(f"Database not found: {db}")
    size_before = db.stat().st_size
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        if args.check:
            info = inspect_hydration_cache(con)
            print(
                "Hydration cache: "
                f"packed-tags={'ready' if info['packed_tags_ready'] else 'not ready'} "
                f"rows={int(info['packed_tags_rows']):,} "
                f"payload={int(info['packed_tags_bytes']) / (1024**2):,.1f} MiB; "
                f"packed-way-ids={'ready' if info['packed_way_ids_ready'] else 'not ready'} "
                f"rows={int(info['packed_way_ids_rows']):,} "
                f"payload={int(info['packed_way_ids_bytes']) / (1024**2):,.1f} MiB"
            )
            return

        if not args.way_ids_only:
            build_packed_tags(con)
        if not args.tags_only:
            build_packed_way_ids(con)
        check = con.execute("PRAGMA quick_check").fetchone()
        print(f"[verify] SQLite quick_check: {check[0] if check else 'no result'}")
    finally:
        con.close()

    size_after = db.stat().st_size
    print(
        f"[hydration-cache] database size delta: {(size_after - size_before) / (1024**2):,.1f} MiB "
        f"(before={size_before / (1024**3):,.2f} GiB after={size_after / (1024**3):,.2f} GiB)"
    )


if __name__ == "__main__":
    main()
