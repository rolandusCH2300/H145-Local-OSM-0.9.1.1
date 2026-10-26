from __future__ import annotations

import sqlite3
import sys
import time
from array import array
from dataclasses import dataclass
from typing import Iterable

from .db import OSMObject
from .tier_c import encode_packed_tags, queue_packed_tag_rows, selector_tag_items


_MEMBER_TYPES = ("node", "way", "relation")
_MEMBER_TYPE_TO_CODE = {name: code for code, name in enumerate(_MEMBER_TYPES)}


@dataclass(slots=True)
class PackedRelationMembers:
    """Lossless compact in-memory representation of relation members.

    Member type is one byte, OSM IDs are stored as signed 64-bit integers and
    roles are interned strings. Iteration deliberately exposes the historical
    ``(type, id, role)`` tuples so the database writer and relation traversal do
    not need to know about the compact representation.
    """

    type_codes: bytes
    member_ids: array
    roles: tuple[str, ...]

    @classmethod
    def pack(cls, members: Iterable[tuple[str, int, str]]) -> "PackedRelationMembers":
        type_codes = bytearray()
        member_ids = array("q")
        roles: list[str] = []
        for member_type, member_id, role in members:
            try:
                type_code = _MEMBER_TYPE_TO_CODE[str(member_type)]
            except KeyError as exc:
                raise ValueError(f"unsupported relation member type: {member_type!r}") from exc
            type_codes.append(type_code)
            member_ids.append(int(member_id))
            roles.append(sys.intern(str(role or "")))
        return cls(bytes(type_codes), member_ids, tuple(roles))

    def __len__(self) -> int:
        return len(self.member_ids)

    def __iter__(self):
        for type_code, member_id, role in zip(self.type_codes, self.member_ids, self.roles):
            yield (_MEMBER_TYPES[type_code], int(member_id), role)


@dataclass(slots=True)
class PendingRelation:
    osm_id: int
    tags: dict[str, str]
    members: PackedRelationMembers | Iterable[tuple[str, int, str]]

    def __post_init__(self) -> None:
        if not isinstance(self.members, PackedRelationMembers):
            self.members = PackedRelationMembers.pack(self.members)


def compact_int_ids(values: Iterable[int]) -> array:
    """Copy exact integer IDs into an 8-byte-per-ID contiguous array."""
    return array("q", (int(value) for value in values))


def insert_new_object_without_rtree(con: sqlite3.Connection, obj: OSMObject) -> int:
    """Insert one schema-v6 object without updating the RTree.

    Full tags are queued to the spool for later loading into SQLite.
    """
    tags_items = tuple((str(k), str(v)) for k, v in obj.tags.items())
    cur = con.execute(
        """INSERT INTO objects
           (osm_type, osm_id, center_lat, center_lon,
            min_lat, max_lat, min_lon, max_lon, area_eligible, has_tags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obj.osm_type, int(obj.osm_id), obj.center_lat, obj.center_lon,
            obj.min_lat, obj.max_lat, obj.min_lon, obj.max_lon,
            int(obj.area_eligible), int(bool(tags_items)),
        ),
    )
    pk = int(cur.lastrowid)
    selector_rows = selector_tag_items(tags_items)
    if selector_rows:
        con.executemany(
            "INSERT INTO object_tags(object_pk,key,value) VALUES(?,?,?)",
            ((pk, key, value) for key, value in selector_rows),
        )
    if tags_items:
        queue_packed_tag_rows(con, ((pk, encode_packed_tags(tags_items)),))
    if obj.osm_type == "node":
        con.execute(
            "INSERT OR IGNORE INTO node_coords(node_id,lat,lon) VALUES(?,?,?)",
            (int(obj.osm_id), obj.center_lat, obj.center_lon),
        )
    return pk


def rebuild_object_rtree(con: sqlite3.Connection) -> tuple[int, float]:
    """Bulk-build the derived object RTree and verify one row per object."""
    started = time.perf_counter()
    con.execute("DELETE FROM object_rtree")
    con.execute(
        """INSERT INTO object_rtree(pk,min_lon,max_lon,min_lat,max_lat)
           SELECT pk,min_lon,max_lon,min_lat,max_lat
           FROM objects
           ORDER BY pk"""
    )
    con.commit()

    object_count = int(con.execute("SELECT COUNT(*) FROM objects").fetchone()[0])
    rtree_count = int(con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0])
    missing = con.execute(
        """SELECT o.pk FROM objects o
           LEFT JOIN object_rtree r ON r.pk=o.pk
           WHERE r.pk IS NULL LIMIT 1"""
    ).fetchone()
    extra = con.execute(
        """SELECT r.pk FROM object_rtree r
           LEFT JOIN objects o ON o.pk=r.pk
           WHERE o.pk IS NULL LIMIT 1"""
    ).fetchone()
    if rtree_count != object_count or missing is not None or extra is not None:
        raise RuntimeError(
            "object RTree rebuild verification failed: "
            f"objects={object_count:,} rtree={rtree_count:,} "
            f"missing={None if missing is None else missing[0]} "
            f"extra={None if extra is None else extra[0]}"
        )

    return rtree_count, time.perf_counter() - started


def verify_foreign_keys(con: sqlite3.Connection) -> None:
    """Reject a staging DB if deferred FK validation finds any orphan."""
    con.commit()
    row = con.execute("PRAGMA foreign_key_check").fetchone()
    if row is None:
        return
    raise RuntimeError(
        "SQLite foreign_key_check failed: "
        f"table={row[0]!r} rowid={row[1]!r} parent={row[2]!r} fk={row[3]!r}"
    )


def location_cache_used_memory(processor) -> int | None:
    """Return libosmium location-table allocation when exposed by pyosmium."""
    try:
        location_store = processor.node_location_storage
    except Exception:
        return None
    if location_store is None:
        return None
    try:
        return int(location_store.used_memory())
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
