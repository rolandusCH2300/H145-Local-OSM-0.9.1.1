from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .tier_c import PACKED_TAGS_TABLE, selector_tag_items


@dataclass(slots=True)
class EquivalenceStats:
    objects: int = 0
    full_tags: int = 0
    selector_tags: int = 0
    way_geometries: int = 0
    relation_members: int = 0
    node_coords: int = 0
    area_geometries: int = 0


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def _meta(con: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(con, "metadata"):
        return None
    row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _schema_version(con: sqlite3.Connection) -> int:
    try:
        return int(_meta(con, "schema_version") or 0)
    except ValueError:
        return 0


def _object_cursor(con: sqlite3.Connection, osm_type: str):
    """Iterate by OSM type and ID; internal primary keys can differ between databases."""
    return con.execute(
        """SELECT pk,osm_type,osm_id,center_lat,center_lon,
                  min_lat,max_lat,min_lon,max_lon,area_eligible
           FROM objects WHERE osm_type=? ORDER BY osm_id,pk""",
        (osm_type,),
    )


def _chunked_cursor(cursor, size: int):
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield rows


def _load_tags_many(
    con: sqlite3.Connection,
    pks: list[int],
    *,
    prefer_packed: bool,
) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {int(pk): {} for pk in pks}
    if not pks:
        return out
    use_packed = prefer_packed and _table_exists(con, PACKED_TAGS_TABLE)
    for offset in range(0, len(pks), 3000):
        chunk = pks[offset:offset + 3000]
        marks = ",".join("?" for _ in chunk)
        if use_packed:
            for row in con.execute(
                f"SELECT object_pk,tags_json FROM {PACKED_TAGS_TABLE} "
                f"WHERE object_pk IN ({marks})",
                chunk,
            ):
                raw = row[1]
                if isinstance(raw, memoryview):
                    raw = raw.tobytes()
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise RuntimeError(f"invalid packed tag payload for object_pk={row[0]}")
                out[int(row[0])] = {str(k): str(v) for k, v in payload.items()}
        else:
            for row in con.execute(
                f"SELECT object_pk,key,value FROM object_tags "
                f"WHERE object_pk IN ({marks}) ORDER BY object_pk,key",
                chunk,
            ):
                out[int(row[0])][str(row[1])] = str(row[2])
    return out


def _load_normalized_tags_many(
    con: sqlite3.Connection, pks: list[int]
) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {int(pk): {} for pk in pks}
    for offset in range(0, len(pks), 3000):
        chunk = pks[offset:offset + 3000]
        if not chunk:
            continue
        marks = ",".join("?" for _ in chunk)
        for row in con.execute(
            f"SELECT object_pk,key,value FROM object_tags "
            f"WHERE object_pk IN ({marks}) ORDER BY object_pk,key",
            chunk,
        ):
            out[int(row[0])][str(row[1])] = str(row[2])
    return out


def _row_payload(row: sqlite3.Row) -> tuple:
    return (
        str(row["osm_type"]),
        int(row["osm_id"]),
        float(row["center_lat"]),
        float(row["center_lon"]),
        float(row["min_lat"]),
        float(row["max_lat"]),
        float(row["min_lon"]),
        float(row["max_lon"]),
        int(row["area_eligible"]),
    )


def _assert_unique_identity(con: sqlite3.Connection, label: str) -> None:
    duplicate = con.execute(
        """SELECT osm_type,osm_id,COUNT(*) AS n
           FROM objects GROUP BY osm_type,osm_id HAVING n>1 LIMIT 1"""
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError(
            f"{label} database contains duplicate OSM identity: "
            f"{duplicate[0]}/{duplicate[1]} count={duplicate[2]}"
        )


def _compare_objects_and_tags(
    old: sqlite3.Connection,
    new: sqlite3.Connection,
    stats: EquivalenceStats,
    *,
    batch_size: int = 5000,
) -> None:
    old_packed = (
        _table_exists(old, PACKED_TAGS_TABLE)
        and _meta(old, "packed_tags_complete") == "1"
    )
    new_packed = (
        _table_exists(new, PACKED_TAGS_TABLE)
        and _meta(new, "packed_tags_complete") == "1"
    )
    if not new_packed:
        raise RuntimeError("new database does not report a complete packed-tag cache")

    _assert_unique_identity(old, "old")
    _assert_unique_identity(new, "new")

    for osm_type in ("node", "way", "relation"):
        old_cursor = _object_cursor(old, osm_type)
        new_cursor = _object_cursor(new, osm_type)
        old_iter = _chunked_cursor(old_cursor, batch_size)
        new_iter = _chunked_cursor(new_cursor, batch_size)

        while True:
            old_rows = next(old_iter, None)
            new_rows = next(new_iter, None)
            if old_rows is None or new_rows is None:
                if old_rows is not None or new_rows is not None:
                    raise RuntimeError(f"{osm_type} object count differs between databases")
                break
            if len(old_rows) != len(new_rows):
                raise RuntimeError(
                    f"{osm_type} object batch length differs: old={len(old_rows)} new={len(new_rows)}"
                )

            old_pks = [int(row["pk"]) for row in old_rows]
            new_pks = [int(row["pk"]) for row in new_rows]
            old_tags = _load_tags_many(old, old_pks, prefer_packed=old_packed)
            new_tags = _load_tags_many(new, new_pks, prefer_packed=True)
            new_selector_tags = _load_normalized_tags_many(new, new_pks)

            for old_row, new_row in zip(old_rows, new_rows):
                old_payload = _row_payload(old_row)
                new_payload = _row_payload(new_row)
                if old_payload != new_payload:
                    raise RuntimeError(
                        "object payload mismatch: "
                        f"old={old_payload!r} new={new_payload!r}"
                    )

                old_pk = int(old_row["pk"])
                new_pk = int(new_row["pk"])
                identity = (osm_type, int(old_row["osm_id"]))

                old_full = old_tags[old_pk]
                new_full = new_tags[new_pk]
                if old_full != new_full:
                    missing = sorted(set(old_full.items()) - set(new_full.items()))[:10]
                    extra = sorted(set(new_full.items()) - set(old_full.items()))[:10]
                    raise RuntimeError(
                        f"full tag mismatch for {identity}: missing={missing!r} extra={extra!r}"
                    )

                expected_selector = dict(selector_tag_items(old_full))
                actual_selector = new_selector_tags[new_pk]
                if expected_selector != actual_selector:
                    missing = sorted(set(expected_selector.items()) - set(actual_selector.items()))[:10]
                    extra = sorted(set(actual_selector.items()) - set(expected_selector.items()))[:10]
                    raise RuntimeError(
                        f"selector tag mismatch for {identity}: missing={missing!r} extra={extra!r}"
                    )

                stats.objects += 1
                stats.full_tags += len(new_full)
                stats.selector_tags += len(actual_selector)

                if stats.objects % 1_000_000 == 0:
                    print(
                        f"[equivalence] objects={stats.objects:,} "
                        f"full-tags={stats.full_tags:,} selector-tags={stats.selector_tags:,}"
                    )


def _compare_ordered_rows(
    old: sqlite3.Connection,
    new: sqlite3.Connection,
    *,
    label: str,
    sql: str,
    batch_size: int = 10000,
) -> int:
    old_cursor = old.execute(sql)
    new_cursor = new.execute(sql)
    count = 0
    while True:
        left = old_cursor.fetchmany(batch_size)
        right = new_cursor.fetchmany(batch_size)
        if len(left) != len(right):
            raise RuntimeError(
                f"{label} row count differs near row {count:,}: old={len(left)} new={len(right)}"
            )
        if not left:
            break
        for old_row, new_row in zip(left, right):
            old_values = tuple(
                bytes(value) if isinstance(value, memoryview) else value
                for value in old_row
            )
            new_values = tuple(
                bytes(value) if isinstance(value, memoryview) else value
                for value in new_row
            )
            if old_values != new_values:
                raise RuntimeError(
                    f"{label} mismatch at row {count:,}: old={old_values!r} new={new_values!r}"
                )
            count += 1
    print(f"[equivalence] {label}: {count:,} rows [OK]")
    return count


def _compare_area_geometries(
    old: sqlite3.Connection, new: sqlite3.Connection
) -> int:
    def rows(con: sqlite3.Connection):
        return list(con.execute(
            """SELECT o.osm_type,o.osm_id,a.geojson
               FROM area_geometries a JOIN objects o ON o.pk=a.object_pk
               ORDER BY o.osm_type,o.osm_id"""
        ))

    old_rows = rows(old)
    new_rows = rows(new)
    if len(old_rows) != len(new_rows):
        raise RuntimeError(
            f"area geometry count differs: old={len(old_rows)} new={len(new_rows)}"
        )
    for left, right in zip(old_rows, new_rows):
        if tuple(left) != tuple(right):
            raise RuntimeError(
                f"area geometry mismatch: old={(left[0], left[1])!r} new={(right[0], right[1])!r}"
            )
    print(f"[equivalence] area-geometries: {len(new_rows):,} rows [OK]")
    return len(new_rows)


def compare_databases(old_path: Path, new_path: Path) -> EquivalenceStats:
    old_path = old_path.resolve()
    new_path = new_path.resolve()
    old = _connect(old_path)
    new = _connect(new_path)
    stats = EquivalenceStats()
    started = time.perf_counter()
    try:
        old_profile = _meta(old, "profile")
        new_profile = _meta(new, "profile")
        if old_profile != new_profile:
            raise RuntimeError(f"profile differs: old={old_profile!r} new={new_profile!r}")

        old_source_size = _meta(old, "source_size_bytes")
        new_source_size = _meta(new, "source_size_bytes")
        print(
            f"[equivalence] schema old={_schema_version(old)} new={_schema_version(new)} "
            f"source-bytes old={old_source_size or '?'} new={new_source_size or '?'}"
        )
        if old_source_size and new_source_size and old_source_size != new_source_size:
            print(
                "[equivalence] WARNING: source PBF sizes differ; exact equivalence may legitimately fail"
            )

        _compare_objects_and_tags(old, new, stats)
        print(
            f"[equivalence] objects/full tags/selectors: {stats.objects:,} / "
            f"{stats.full_tags:,} / {stats.selector_tags:,} [OK]"
        )

        stats.way_geometries = _compare_ordered_rows(
            old,
            new,
            label="way-geometry-v5",
            sql="SELECT way_id,node_ids,coordinates FROM way_geometry_v5 ORDER BY way_id",
        )
        stats.relation_members = _compare_ordered_rows(
            old,
            new,
            label="relation-members",
            sql=(
                "SELECT relation_id,seq,member_type,member_id,role "
                "FROM relation_members ORDER BY relation_id,seq"
            ),
        )
        stats.node_coords = _compare_ordered_rows(
            old,
            new,
            label="node-coords",
            sql="SELECT node_id,lat,lon FROM node_coords ORDER BY node_id",
        )
        stats.area_geometries = _compare_area_geometries(old, new)

        elapsed = time.perf_counter() - started
        old_size = old_path.stat().st_size
        new_size = new_path.stat().st_size
        selector_reduction = (
            100.0 * (1.0 - stats.selector_tags / stats.full_tags)
            if stats.full_tags else 0.0
        )
        print(
            f"[equivalence] PASS in {elapsed:,.1f}s; DB size "
            f"{old_size / (1024**3):,.2f} -> {new_size / (1024**3):,.2f} GiB "
            f"({100.0 * (new_size / old_size - 1.0):+.1f}%); "
            f"normalized tag rows reduced {selector_reduction:.1f}% vs full-tag count"
        )
        return stats
    finally:
        old.close()
        new.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare pre/post optimization Local OSM databases semantically"
    )
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        compare_databases(args.old, args.new)
    except Exception as exc:
        print(f"[equivalence] FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
