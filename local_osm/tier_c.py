from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
import time
from typing import Iterable

from .import_profile import ANY_VALUE_KEYS, EXACT_VALUES, PROFILE_NAME

SCHEMA_VERSION = 6
OBJECT_IDENTITY_VERSION = 2
SELECTOR_TAGS_VERSION = 1
PACKED_TAGS_VERSION = 1
PACKED_TAGS_TABLE = "packed_object_tags"


TIER_C_OBJECT_SCHEMA = r"""
CREATE TABLE objects (
    pk          INTEGER PRIMARY KEY,
    osm_type    TEXT NOT NULL CHECK (osm_type IN ('node','way','relation')),
    osm_id      INTEGER NOT NULL,
    center_lat  REAL NOT NULL,
    center_lon  REAL NOT NULL,
    min_lat     REAL NOT NULL,
    max_lat     REAL NOT NULL,
    min_lon     REAL NOT NULL,
    max_lon     REAL NOT NULL,
    area_eligible INTEGER NOT NULL DEFAULT 0 CHECK (area_eligible IN (0,1)),
    has_tags    INTEGER NOT NULL DEFAULT 0 CHECK (has_tags IN (0,1))
);

CREATE VIRTUAL TABLE object_rtree USING rtree(
    pk,
    min_lon, max_lon,
    min_lat, max_lat
);

CREATE TABLE object_tags (
    object_pk INTEGER NOT NULL REFERENCES objects(pk) ON DELETE CASCADE,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (object_pk, key)
) WITHOUT ROWID;

CREATE TABLE area_geometries (
    object_pk INTEGER PRIMARY KEY REFERENCES objects(pk) ON DELETE CASCADE,
    geojson   TEXT NOT NULL
);

CREATE TABLE packed_object_tags (
    object_pk INTEGER PRIMARY KEY REFERENCES objects(pk) ON DELETE CASCADE,
    tags_json BLOB NOT NULL
);

-- In schema v6 this table is authoritative full-tag data. object_tags contains
-- only the searchable selector subset, so the legacy hydration-cache rebuild
-- utility must never clear and regenerate packed tags from object_tags.
CREATE TRIGGER protect_tier_c_packed_tags_delete
BEFORE DELETE ON packed_object_tags
BEGIN
    SELECT RAISE(ABORT,
        'Tier-C packed_object_tags stores authoritative full tags; rebuild from PBF instead');
END;
"""

WAY_GEOMETRY_V5_SCHEMA = r"""
CREATE TABLE way_geometry_v5 (
    way_id      INTEGER PRIMARY KEY,
    node_ids    BLOB NOT NULL,
    coordinates BLOB NOT NULL
)
"""


class PackedTagSpool:
    """Append-only temporary full-tag stream used during immutable imports.

    The hot SQLite writer should not maintain another B-tree containing hundreds
    of MiB of JSON while it is already writing objects, geometry and selector
    tags. Full tags are therefore encoded once, appended sequentially here, and
    bulk-loaded into packed_object_tags after topology/finalization.
    """

    _HEADER = struct.Struct("<QI")

    def __init__(self) -> None:
        self.file = tempfile.TemporaryFile(mode="w+b")
        self.rows = 0
        self.payload_bytes = 0

    def write_rows(self, rows: Iterable[tuple[int, bytes]]) -> None:
        buffer = bytearray()
        count = 0
        payload_bytes = 0
        for pk, payload in rows:
            data = bytes(payload)
            buffer.extend(self._HEADER.pack(int(pk), len(data)))
            buffer.extend(data)
            count += 1
            payload_bytes += len(data)
        if buffer:
            self.file.write(buffer)
            self.rows += count
            self.payload_bytes += payload_bytes

    def load_into(self, con: sqlite3.Connection, *, batch_size: int = 50_000) -> tuple[int, int]:
        self.file.flush()
        self.file.seek(0)
        insert_sql = (
            f"INSERT INTO {PACKED_TAGS_TABLE}(object_pk,tags_json) VALUES(?,?)"
        )
        batch: list[tuple[int, bytes]] = []
        rows = 0
        payload_bytes = 0
        header_size = self._HEADER.size
        while True:
            header = self.file.read(header_size)
            if not header:
                break
            if len(header) != header_size:
                raise RuntimeError("truncated Tier-C packed-tag spool header")
            pk, size = self._HEADER.unpack(header)
            payload = self.file.read(size)
            if len(payload) != size:
                raise RuntimeError("truncated Tier-C packed-tag spool payload")
            batch.append((int(pk), payload))
            rows += 1
            payload_bytes += size
            if len(batch) >= batch_size:
                con.executemany(insert_sql, batch)
                batch.clear()
        if batch:
            con.executemany(insert_sql, batch)
        if rows != self.rows or payload_bytes != self.payload_bytes:
            raise RuntimeError(
                "Tier-C packed-tag spool accounting mismatch: "
                f"queued={self.rows:,}/{self.payload_bytes:,} "
                f"loaded={rows:,}/{payload_bytes:,}"
            )
        return rows, payload_bytes

    def close(self) -> None:
        self.file.close()


_PACKED_SPOOLS: dict[int, PackedTagSpool] = {}


def _start_spool(con: sqlite3.Connection) -> PackedTagSpool:
    key = id(con)
    previous = _PACKED_SPOOLS.pop(key, None)
    if previous is not None:
        previous.close()
    spool = PackedTagSpool()
    _PACKED_SPOOLS[key] = spool
    return spool


def queue_packed_tag_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, bytes]]) -> None:
    spool = _PACKED_SPOOLS.get(id(con))
    if spool is None:
        raise RuntimeError("Tier-C packed-tag spool is not active")
    spool.write_rows(rows)


def discard_packed_tag_spool(con: sqlite3.Connection) -> None:
    spool = _PACKED_SPOOLS.pop(id(con), None)
    if spool is not None:
        spool.close()


def _as_tag_items(tags: Iterable[tuple[str, str]] | dict[str, str]) -> tuple[tuple[str, str], ...]:
    if isinstance(tags, dict):
        return tuple((str(key), str(value)) for key, value in tags.items())
    return tuple((str(key), str(value)) for key, value in tags)


def is_country_boundary_items(tags: Iterable[tuple[str, str]] | dict[str, str]) -> bool:
    items = _as_tag_items(tags)
    boundary = False
    level = False
    for key, value in items:
        if key == "boundary" and value == "administrative":
            boundary = True
        elif key == "admin_level" and value == "2":
            level = True
    return boundary and level


def selector_tag_items(
    tags: Iterable[tuple[str, str]] | dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Return only normalized tags needed by the frozen query profile.

    Every original tag is preserved separately in packed_object_tags. Country
    relations intentionally retain all normalized tags too because the is_in
    compatibility path and standalone country-geometry tooling read them via
    legacy get_tags()/get_tags_many() helpers.
    """
    items = _as_tag_items(tags)
    if is_country_boundary_items(items):
        return items

    out: list[tuple[str, str]] = []
    for key, value in items:
        if key in ANY_VALUE_KEYS:
            out.append((key, value))
            continue
        allowed = EXACT_VALUES.get(key)
        if allowed is not None and value in allowed:
            out.append((key, value))
    return tuple(out)


def encode_packed_tags(
    tags: Iterable[tuple[str, str]] | dict[str, str],
) -> bytes:
    """Encode every original tag as compact Unicode-safe JSON.

    Key sorting is deliberately avoided: OSM tags have no semantic ordering and
    sorting millions of tiny dictionaries only burns CPU in the import path.
    """
    items = _as_tag_items(tags)
    payload = {key: value for key, value in items}
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def prepare_tier_c_schema(con: sqlite3.Connection) -> None:
    """Replace empty compatibility tables with the finalized import-v6 layout."""
    existing = int(con.execute("SELECT COUNT(*) FROM objects").fetchone()[0])
    if existing:
        raise RuntimeError(
            f"Tier-C schema preparation requires an empty objects table, found {existing:,} rows"
        )
    geometry_rows = int(con.execute("SELECT COUNT(*) FROM way_geometry_v5").fetchone()[0])
    if geometry_rows:
        raise RuntimeError(
            "Tier-C schema preparation requires an empty way_geometry_v5 table, "
            f"found {geometry_rows:,} rows"
        )

    con.commit()
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP TABLE IF EXISTS object_rtree")
    con.execute("DROP TABLE IF EXISTS packed_object_tags")
    con.execute("DROP TABLE IF EXISTS object_tags")
    con.execute("DROP TABLE IF EXISTS area_geometries")
    con.execute("DROP TABLE IF EXISTS objects")
    con.execute("DROP TABLE IF EXISTS way_geometry_v5")
    con.executescript(TIER_C_OBJECT_SCHEMA)
    con.execute(WAY_GEOMETRY_V5_SCHEMA)
    con.commit()
    _start_spool(con)


def finalize_tier_c_tags(con: sqlite3.Connection) -> tuple[int, int, float]:
    """Bulk-load the full-tag spool, verify completeness and publish metadata."""
    started = time.perf_counter()
    con.commit()
    spool = _PACKED_SPOOLS.pop(id(con), None)
    if spool is None:
        raise RuntimeError("Tier-C packed-tag spool is not active at finalization")
    try:
        existing = int(con.execute(f"SELECT COUNT(*) FROM {PACKED_TAGS_TABLE}").fetchone()[0])
        if existing:
            raise RuntimeError(
                f"Tier-C packed tag table must be empty before bulk load, found {existing:,} rows"
            )
        packed_rows, payload_bytes = spool.load_into(con)
        con.commit()
    finally:
        spool.close()

    tagged_rows = int(con.execute("SELECT COUNT(*) FROM objects WHERE has_tags=1").fetchone()[0])
    if packed_rows != tagged_rows:
        missing = con.execute(
            f"""SELECT o.pk,o.osm_type,o.osm_id
                FROM objects o
                LEFT JOIN {PACKED_TAGS_TABLE} p ON p.object_pk=o.pk
                WHERE o.has_tags=1 AND p.object_pk IS NULL LIMIT 1"""
        ).fetchone()
        extra = con.execute(
            f"""SELECT p.object_pk
                FROM {PACKED_TAGS_TABLE} p
                JOIN objects o ON o.pk=p.object_pk
                WHERE o.has_tags=0 LIMIT 1"""
        ).fetchone()
        raise RuntimeError(
            "packed tag completeness check failed: "
            f"tagged={tagged_rows:,} packed={packed_rows:,} "
            f"missing={tuple(missing) if missing else None} "
            f"extra={None if extra is None else extra[0]}"
        )

    selector_rows = int(con.execute("SELECT COUNT(*) FROM object_tags").fetchone()[0])
    metadata = {
        "packed_tags_version": PACKED_TAGS_VERSION,
        "packed_tags_complete": 1,
        "packed_tags_rows": packed_rows,
        "packed_tags_bytes": payload_bytes,
        "selector_tags_version": SELECTOR_TAGS_VERSION,
        "selector_tags_complete": 1,
        "selector_tags_profile": PROFILE_NAME,
        "selector_tags_rows": selector_rows,
        "object_identity_version": OBJECT_IDENTITY_VERSION,
    }
    con.executemany(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ((key, str(value)) for key, value in metadata.items()),
    )
    con.commit()
    return packed_rows, payload_bytes, time.perf_counter() - started


def verify_tier_c_identity(con: sqlite3.Connection) -> None:
    """Reject a build if the same OSM type/id identity appears more than once."""
    duplicate = con.execute(
        """SELECT a.pk,a.osm_type,a.osm_id,b.pk
           FROM objects a
           JOIN objects b ON b.osm_id=a.osm_id
                         AND b.osm_type=a.osm_type
                         AND b.pk<a.pk
           LIMIT 1"""
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError(
            "OSM object identity uniqueness check failed: "
            f"type={duplicate[1]!r} osm_id={duplicate[2]} "
            f"pks={duplicate[3]},{duplicate[0]}"
        )


def selector_contract_sql_violations(con: sqlite3.Connection) -> int:
    """Count non-country normalized rows outside the frozen selector profile."""
    violations = 0
    rows = con.execute(
        """SELECT t.object_pk,t.key,t.value,
                  EXISTS(SELECT 1 FROM object_tags b
                         WHERE b.object_pk=t.object_pk AND b.key='boundary' AND b.value='administrative') AS is_boundary,
                  EXISTS(SELECT 1 FROM object_tags a
                         WHERE a.object_pk=t.object_pk AND a.key='admin_level' AND a.value='2') AS is_level2
           FROM object_tags t"""
    )
    for row in rows:
        if row[3] and row[4]:
            continue
        key = str(row[1])
        value = str(row[2])
        if key in ANY_VALUE_KEYS:
            continue
        allowed = EXACT_VALUES.get(key)
        if allowed is not None and value in allowed:
            continue
        violations += 1
    return violations
