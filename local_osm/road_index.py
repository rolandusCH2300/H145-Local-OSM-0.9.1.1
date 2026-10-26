from __future__ import annotations

import argparse
import array
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Iterable

from .db import decode_way_geometry_node_ids, decode_way_node_ids

# Version 2 adds a derived fixed-width node-ID stream to the road index.
ROAD_INDEX_VERSION = 2
ROAD_INDEX_BASE_VERSION = 1

# Compact stable codes for the highway values used by R&E's road/intersection
# queries. Keeping the persistent index numeric avoids duplicating tag strings.
ROAD_CLASS_BY_VALUE: dict[str, int] = {
    "motorway": 1,
    "trunk": 2,
    "primary": 3,
    "secondary": 4,
    "tertiary": 5,
    "motorway_link": 6,
    "trunk_link": 7,
    "primary_link": 8,
    "secondary_link": 9,
    "tertiary_link": 10,
    "unclassified": 11,
    "residential": 12,
    "living_street": 13,
    "service": 14,
}
ROAD_VALUE_BY_CLASS = {v: k for k, v in ROAD_CLASS_BY_VALUE.items()}
ROAD_VALUES = tuple(ROAD_CLASS_BY_VALUE)

ROAD_INDEX_DDL = r"""
CREATE TABLE IF NOT EXISTS road_ways (
    way_id          INTEGER PRIMARY KEY,
    road_class      INTEGER NOT NULL,
    node_ids_fixed  BLOB
);

CREATE VIRTUAL TABLE IF NOT EXISTS road_way_rtree USING rtree(
    way_id,
    min_lon, max_lon,
    min_lat, max_lat
);
"""


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    if not table_exists(con, table):
        return False
    return any(str(row[1]) == column for row in con.execute(f"PRAGMA table_info({table})"))


def _metadata_int(con: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return default if row is None else int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        return default


def road_index_version(con: sqlite3.Connection) -> int:
    if not (table_exists(con, "road_ways") and table_exists(con, "road_way_rtree")):
        return 0
    return _metadata_int(con, "road_index_version", 0)


def road_index_ready(con: sqlite3.Connection) -> bool:
    """Return whether a supported derived road index is available."""
    version = road_index_version(con)
    if version < ROAD_INDEX_BASE_VERSION or version > ROAD_INDEX_VERSION:
        return False
    try:
        return con.execute("SELECT 1 FROM road_ways LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def road_index_fast_nodes_ready(con: sqlite3.Connection) -> bool:
    """Return True only when every indexed road has the v2 fixed-ID payload."""
    if road_index_version(con) < 2 or not _column_exists(con, "road_ways", "node_ids_fixed"):
        return False
    try:
        rows = _metadata_int(con, "road_index_rows", -1)
        fast_rows = _metadata_int(con, "road_index_fast_node_rows", -2)
        return rows >= 0 and fast_rows == rows
    except (sqlite3.Error, TypeError, ValueError):
        return False


def encode_fixed_node_ids(node_ids: Iterable[int]) -> bytes:
    """Encode node IDs as fixed-width little-endian unsigned 64-bit values."""
    values = array.array("Q", (int(v) for v in node_ids))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def fixed_node_ids_view(blob: bytes | memoryview | None):
    """Return a zero-copy iterable of v2 node IDs on little-endian hosts."""
    if blob is None:
        return ()
    data = blob if isinstance(blob, (bytes, bytearray)) else bytes(blob)
    if len(data) % 8:
        raise ValueError("invalid road-index v2 node-ID payload length")
    if not data:
        return ()
    if sys.byteorder == "little":
        return memoryview(data).cast("Q")
    # The application is Windows/x86 in normal use, but retain portable decode.
    return tuple(value[0] for value in struct.iter_unpack("<Q", data))


def _ensure_v2_column(con: sqlite3.Connection) -> None:
    # Add the optional v2 payload column when upgrading a v1 index.
    if not _column_exists(con, "road_ways", "node_ids_fixed"):
        con.execute("ALTER TABLE road_ways ADD COLUMN node_ids_fixed BLOB")
        con.commit()


def _populate_fast_node_payloads(
    con: sqlite3.Connection,
    *,
    verbose: bool,
    batch_size: int = 4000,
) -> tuple[int, int]:
    """Populate fixed-width road node IDs from available packed geometry."""
    has_v5 = table_exists(con, "way_geometry_v5")
    has_v4 = table_exists(con, "way_geometry")
    if not has_v5 and not has_v4:
        return 0, 0

    v5_join = "LEFT JOIN way_geometry_v5 v5 ON v5.way_id=rw.way_id" if has_v5 else ""
    v4_join = "LEFT JOIN way_geometry v4 ON v4.way_id=rw.way_id" if has_v4 else ""
    v5_col = "v5.node_ids" if has_v5 else "NULL"
    v4_col = "v4.node_data" if has_v4 else "NULL"
    source_sql = f"""
        SELECT rw.way_id,{v5_col} AS v5_ids,{v4_col} AS v4_data
        FROM road_ways rw
        {v5_join}
        {v4_join}
        ORDER BY rw.way_id
    """

    updated = 0
    payload_bytes = 0
    batch: list[tuple[bytes, int]] = []
    for row in con.execute(source_sql):
        way_id = int(row[0])
        if row[1] is not None:
            ids = decode_way_node_ids(row[1])
        elif row[2] is not None:
            ids = decode_way_geometry_node_ids(row[2])
        else:
            # Older schema generations have no compact geometry stream. Leave
            # the payload NULL; the completed index will remain version 1.
            continue
        payload = encode_fixed_node_ids(ids)
        payload_bytes += len(payload)
        batch.append((payload, way_id))
        if len(batch) >= batch_size:
            con.executemany(
                "UPDATE road_ways SET node_ids_fixed=? WHERE way_id=?",
                batch,
            )
            updated += len(batch)
            batch.clear()
            if verbose and updated % 200_000 < batch_size:
                print(f"[road-index] fixed node IDs: {updated:,} ways")
    if batch:
        con.executemany(
            "UPDATE road_ways SET node_ids_fixed=? WHERE way_id=?",
            batch,
        )
        updated += len(batch)

    return updated, payload_bytes


def build_road_index(con: sqlite3.Connection, *, verbose: bool = True) -> tuple[int, float]:
    """Build or rebuild the derived road spatial index and node-ID accelerator."""
    started = time.perf_counter()
    values = ROAD_VALUES
    placeholders = ",".join("?" for _ in values)
    case_sql = "CASE t.value " + " ".join(
        f"WHEN {repr(value)} THEN {code}" for value, code in ROAD_CLASS_BY_VALUE.items()
    ) + " END"

    if verbose:
        print("[road-index] building compact R&E road-way index...")

    con.executescript(ROAD_INDEX_DDL)
    _ensure_v2_column(con)

    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM road_way_rtree")
        con.execute("DELETE FROM road_ways")
        con.execute(
            f"""
            INSERT INTO road_ways(way_id, road_class)
            SELECT o.osm_id, {case_sql}
            FROM object_tags t INDEXED BY idx_tags_key_value
            JOIN objects o ON o.pk=t.object_pk
            WHERE t.key='highway'
              AND t.value IN ({placeholders})
              AND o.osm_type='way'
            """,
            values,
        )
        con.execute(
            """
            INSERT INTO road_way_rtree(way_id,min_lon,max_lon,min_lat,max_lat)
            SELECT rw.way_id,o.min_lon,o.max_lon,o.min_lat,o.max_lat
            FROM road_ways rw
            JOIN objects o ON o.osm_type='way' AND o.osm_id=rw.way_id
            """
        )
        count = int(con.execute("SELECT COUNT(*) FROM road_ways").fetchone()[0])

        fast_started = time.perf_counter()
        fast_rows, payload_bytes = _populate_fast_node_payloads(con, verbose=verbose)
        fast_elapsed = time.perf_counter() - fast_started
        target_version = 2 if count > 0 and fast_rows == count else 1

        con.execute(
            "INSERT INTO metadata(key,value) VALUES('road_index_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(target_version),),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('road_index_rows',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(count),),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('road_index_fast_node_rows',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(fast_rows if target_version >= 2 else 0),),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('road_index_fast_node_bytes',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(payload_bytes if target_version >= 2 else 0),),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise

    elapsed = time.perf_counter() - started
    if verbose:
        print(
            f"[road-index] complete: ways={count:,} version={target_version} "
            f"elapsed={elapsed:,.1f}s"
        )
        if target_version >= 2:
            print(
                f"[road-index] v2 fixed IDs: rows={fast_rows:,} "
                f"payload={payload_bytes / (1024 * 1024):,.1f} MiB "
                f"build={fast_elapsed:,.1f}s"
            )
    return count, elapsed


def inspect_road_index(con: sqlite3.Connection) -> dict[str, int | bool]:
    ready = road_index_ready(con)
    rows = 0
    if table_exists(con, "road_ways"):
        rows = int(con.execute("SELECT COUNT(*) FROM road_ways").fetchone()[0])
    version = road_index_version(con) if ready else 0
    return {
        "ready": ready,
        "rows": rows,
        "version": version,
        "fast_nodes_ready": road_index_fast_nodes_ready(con),
        "fast_node_rows": _metadata_int(con, "road_index_fast_node_rows", 0),
        "fast_node_bytes": _metadata_int(con, "road_index_fast_node_bytes", 0),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Build/upgrade the optional compact R&E road-way index without re-importing OSM."
    )
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--check", action="store_true", help="Only report current road-index status")
    args = p.parse_args()
    db = args.db.resolve()
    if not db.is_file():
        raise SystemExit(f"Database not found: {db}")

    size_before = db.stat().st_size
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        if args.check:
            info = inspect_road_index(con)
            print(
                f"Road index: {'ready' if info['ready'] else 'not ready'}; "
                f"rows={int(info['rows']):,}; version={int(info['version'])}; "
                f"fast_nodes={'ready' if info['fast_nodes_ready'] else 'not ready'}; "
                f"fast_rows={int(info['fast_node_rows']):,}; "
                f"fast_payload={int(info['fast_node_bytes']) / (1024 * 1024):,.1f} MiB"
            )
            return
        build_road_index(con)
        check = con.execute("PRAGMA quick_check").fetchone()
        print(f"[verify] SQLite quick_check: {check[0] if check else 'no result'}")
        size_after = db.stat().st_size
        delta = size_after - size_before
        print(
            f"[road-index] database size delta: {delta / (1024 * 1024):+,.2f} MiB "
            f"({size_before / (1024**3):,.2f} -> {size_after / (1024**3):,.2f} GiB)"
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
