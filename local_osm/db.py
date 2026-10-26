from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS objects (
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
    UNIQUE(osm_type, osm_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS object_rtree USING rtree(
    pk,
    min_lon, max_lon,
    min_lat, max_lat
);

CREATE TABLE IF NOT EXISTS object_tags (
    object_pk INTEGER NOT NULL REFERENCES objects(pk) ON DELETE CASCADE,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (object_pk, key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_tags_key_value ON object_tags(key, value, object_pk);
CREATE INDEX IF NOT EXISTS idx_tags_key ON object_tags(key, object_pk);

CREATE TABLE IF NOT EXISTS node_coords (
    node_id INTEGER PRIMARY KEY,
    lat     REAL NOT NULL,
    lon     REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS area_geometries (
    object_pk INTEGER PRIMARY KEY REFERENCES objects(pk) ON DELETE CASCADE,
    geojson   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS way_nodes (
    way_id   INTEGER NOT NULL,
    node_id  INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    PRIMARY KEY (way_id, seq)
) WITHOUT ROWID;

-- Schema v3: one compact varint blob per way. The legacy way_nodes table is
-- retained for backward compatibility with schema-v2 databases and demo data.
CREATE TABLE IF NOT EXISTS way_topology (
    way_id   INTEGER PRIMARY KEY,
    node_ids BLOB NOT NULL
) WITHOUT ROWID;

-- Schema v4: ordered node IDs and coordinates are stored together in one
-- compact blob per way. Ordinary way nodes therefore no longer need global
-- node_coords rows. Legacy topology tables remain readable for older DBs.
CREATE TABLE IF NOT EXISTS way_geometry (
    way_id    INTEGER PRIMARY KEY,
    node_data BLOB NOT NULL
) WITHOUT ROWID;

-- Schema v5: node IDs and coordinates are stored as two aligned packed
-- streams. Non-recursive way output can fetch only node_ids, while recursive
-- expansion reads both streams. The legacy schema-v4 table remains readable.
CREATE TABLE IF NOT EXISTS way_geometry_v5 (
    way_id      INTEGER PRIMARY KEY,
    node_ids    BLOB NOT NULL,
    coordinates BLOB NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS relation_members (
    relation_id INTEGER NOT NULL,
    member_type TEXT NOT NULL CHECK (member_type IN ('node','way','relation')),
    member_id   INTEGER NOT NULL,
    role        TEXT NOT NULL DEFAULT '',
    seq         INTEGER NOT NULL,
    PRIMARY KEY (relation_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_relation_members_member ON relation_members(member_type, member_id, relation_id);

CREATE TABLE IF NOT EXISTS planner_key_stats (
    key          TEXT PRIMARY KEY,
    object_count INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS planner_value_stats (
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    object_count INTEGER NOT NULL,
    PRIMARY KEY (key, value)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

@dataclass(slots=True)
class OSMObject:
    osm_type: str
    osm_id: int
    center_lat: float
    center_lon: float
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    tags: dict[str, str]
    area_eligible: bool = False


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def open_existing_query_db(path: str | Path) -> sqlite3.Connection:
    """Open an existing imported database for the HTTP query server.

    Unlike ``connect()``, this intentionally does not execute schema DDL on
    every runtime connection. The importer/server bootstrap is responsible for
    creating/upgrading schema; request handling is read-only.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    return con




def sqlite_in_chunk_size(con: sqlite3.Connection, *, fixed_params: int = 0, preferred: int = 4000) -> int:
    """Choose a safe IN-clause batch size for the active SQLite build."""
    try:
        limit = int(con.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    except (AttributeError, TypeError, ValueError):
        limit = 999
    usable = max(1, limit - int(fixed_params) - 8)
    return max(1, min(int(preferred), usable))

def insert_object(con: sqlite3.Connection, obj: OSMObject) -> int:
    con.execute(
        """INSERT INTO objects
           (osm_type, osm_id, center_lat, center_lon,
            min_lat, max_lat, min_lon, max_lon, area_eligible)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(osm_type, osm_id) DO UPDATE SET
             center_lat=excluded.center_lat,
             center_lon=excluded.center_lon,
             min_lat=excluded.min_lat,
             max_lat=excluded.max_lat,
             min_lon=excluded.min_lon,
             max_lon=excluded.max_lon,
             area_eligible=excluded.area_eligible""",
        (
            obj.osm_type, obj.osm_id, obj.center_lat, obj.center_lon,
            obj.min_lat, obj.max_lat, obj.min_lon, obj.max_lon,
            int(obj.area_eligible),
        ),
    )
    row = con.execute(
        "SELECT pk FROM objects WHERE osm_type=? AND osm_id=?",
        (obj.osm_type, obj.osm_id),
    ).fetchone()
    pk = int(row["pk"])
    con.execute("DELETE FROM object_rtree WHERE pk=?", (pk,))
    con.execute(
        "INSERT INTO object_rtree(pk,min_lon,max_lon,min_lat,max_lat) VALUES(?,?,?,?,?)",
        (pk, obj.min_lon, obj.max_lon, obj.min_lat, obj.max_lat),
    )
    con.execute("DELETE FROM object_tags WHERE object_pk=?", (pk,))
    con.executemany(
        "INSERT INTO object_tags(object_pk,key,value) VALUES(?,?,?)",
        ((pk, str(k), str(v)) for k, v in obj.tags.items()),
    )
    if obj.osm_type == "node":
        con.execute(
            "INSERT INTO node_coords(node_id,lat,lon) VALUES(?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon",
            (obj.osm_id, obj.center_lat, obj.center_lon),
        )
    return pk


def insert_node_coords(con: sqlite3.Connection, rows: Iterable[tuple[int, float, float]]) -> None:
    con.executemany(
        "INSERT INTO node_coords(node_id,lat,lon) VALUES(?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon",
        ((int(node_id), float(lat), float(lon)) for node_id, lat, lon in rows),
    )


def insert_node_coords_ignore(con: sqlite3.Connection, rows: Iterable[tuple[int, float, float]]) -> None:
    """Insert immutable snapshot node coordinates without rewriting duplicates."""
    con.executemany(
        "INSERT OR IGNORE INTO node_coords(node_id,lat,lon) VALUES(?,?,?)",
        ((int(node_id), float(lat), float(lon)) for node_id, lat, lon in rows),
    )


def insert_new_object(con: sqlite3.Connection, obj: OSMObject) -> int:
    """Fast path for a freshly-created import database.

    Unlike insert_object(), this deliberately has no UPSERT/DELETE work. A
    duplicate object ID is an importer bug and should fail loudly.
    """
    cur = con.execute(
        """INSERT INTO objects
           (osm_type, osm_id, center_lat, center_lon,
            min_lat, max_lat, min_lon, max_lon, area_eligible)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obj.osm_type, obj.osm_id, obj.center_lat, obj.center_lon,
            obj.min_lat, obj.max_lat, obj.min_lon, obj.max_lon,
            int(obj.area_eligible),
        ),
    )
    pk = int(cur.lastrowid)
    con.execute(
        "INSERT INTO object_rtree(pk,min_lon,max_lon,min_lat,max_lat) VALUES(?,?,?,?,?)",
        (pk, obj.min_lon, obj.max_lon, obj.min_lat, obj.max_lat),
    )
    if obj.tags:
        con.executemany(
            "INSERT INTO object_tags(object_pk,key,value) VALUES(?,?,?)",
            ((pk, str(k), str(v)) for k, v in obj.tags.items()),
        )
    if obj.osm_type == "node":
        con.execute(
            "INSERT OR IGNORE INTO node_coords(node_id,lat,lon) VALUES(?,?,?)",
            (obj.osm_id, obj.center_lat, obj.center_lon),
        )
    return pk


def _put_uvarint(out: bytearray, value: int) -> None:
    value = int(value)
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def encode_way_node_ids(node_ids: Iterable[int]) -> bytes:
    """Encode ordered OSM node IDs using delta + zig-zag varints.

    Way node IDs are positive but not monotonic, so the first ID is stored as
    an unsigned varint and subsequent signed deltas use zig-zag encoding.
    This keeps one SQLite row per way while preserving exact node order.
    """
    values = [int(v) for v in node_ids]
    if not values:
        return b""
    out = bytearray()
    _put_uvarint(out, values[0])
    prev = values[0]
    for value in values[1:]:
        delta = value - prev
        zigzag = (delta << 1) ^ (delta >> 63)
        _put_uvarint(out, zigzag)
        prev = value
    return bytes(out)


def _read_uvarint(blob: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    n = len(blob)
    while offset < n:
        byte = blob[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if shift > 70:
            raise ValueError("invalid varint in way topology blob")
    raise ValueError("truncated varint in way topology blob")


def decode_way_node_ids(blob: bytes | memoryview | None) -> list[int]:
    if blob is None:
        return []
    data = bytes(blob)
    if not data:
        return []
    first, offset = _read_uvarint(data, 0)
    out = [first]
    prev = first
    while offset < len(data):
        zigzag, offset = _read_uvarint(data, offset)
        delta = (zigzag >> 1) ^ -(zigzag & 1)
        prev += delta
        out.append(prev)
    return out


COORD_SCALE = 10_000_000


def _zigzag_encode(value: int) -> int:
    value = int(value)
    return (value << 1) ^ (value >> 63)


def _zigzag_decode(value: int) -> int:
    value = int(value)
    return (value >> 1) ^ -(value & 1)


def _coord_to_int(value: float) -> int:
    return int(round(float(value) * COORD_SCALE))


def _coord_from_int(value: int) -> float:
    return int(value) / COORD_SCALE


def encode_way_geometry(nodes: Iterable[tuple[int, float | None, float | None]]) -> bytes:
    """Encode ordered ``(node_id, lat, lon)`` records for schema v4.

    IDs use the same delta + zig-zag varint scheme as schema v3. Coordinates
    use OSM's native 1e-7 degree precision and delta encoding. Zero is reserved
    for a missing coordinate; valid signed values are zig-zag encoded plus one.
    The previous valid coordinate remains the delta base across a missing node.
    """
    values = list(nodes)
    if not values:
        return b""
    out = bytearray()
    prev_id: int | None = None
    prev_lat = 0
    prev_lon = 0
    have_coord = False
    for raw_id, lat, lon in values:
        node_id = int(raw_id)
        if prev_id is None:
            _put_uvarint(out, node_id)
        else:
            _put_uvarint(out, _zigzag_encode(node_id - prev_id))
        prev_id = node_id

        if lat is None or lon is None:
            _put_uvarint(out, 0)
            _put_uvarint(out, 0)
            continue

        lat_i = _coord_to_int(lat)
        lon_i = _coord_to_int(lon)
        if have_coord:
            lat_value = lat_i - prev_lat
            lon_value = lon_i - prev_lon
        else:
            lat_value = lat_i
            lon_value = lon_i
        _put_uvarint(out, _zigzag_encode(lat_value) + 1)
        _put_uvarint(out, _zigzag_encode(lon_value) + 1)
        prev_lat, prev_lon = lat_i, lon_i
        have_coord = True
    return bytes(out)


def decode_way_geometry(blob: bytes | memoryview | None) -> list[tuple[int, float | None, float | None]]:
    """Decode schema-v4 packed way geometry."""
    if blob is None:
        return []
    data = blob if isinstance(blob, bytes) else bytes(blob)
    n = len(data)
    if not n:
        return []

    out: list[tuple[int, float | None, float | None]] = []
    offset = 0
    prev_id: int | None = None
    prev_lat = 0
    prev_lon = 0
    have_coord = False
    scale = float(COORD_SCALE)

    while offset < n:
        raw_id = 0
        shift = 0
        while True:
            byte = data[offset]
            offset += 1
            raw_id |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid varint in way geometry blob")

        if prev_id is None:
            node_id = raw_id
        else:
            node_id = prev_id + ((raw_id >> 1) ^ -(raw_id & 1))
        prev_id = node_id

        lat_code = 0
        shift = 0
        while True:
            byte = data[offset]
            offset += 1
            lat_code |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid varint in way geometry blob")

        lon_code = 0
        shift = 0
        while True:
            byte = data[offset]
            offset += 1
            lon_code |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid varint in way geometry blob")

        if lat_code == 0 or lon_code == 0:
            if lat_code != 0 or lon_code != 0:
                raise ValueError("invalid partial coordinate in way geometry blob")
            out.append((node_id, None, None))
            continue

        lat_raw = lat_code - 1
        lon_raw = lon_code - 1
        lat_value = (lat_raw >> 1) ^ -(lat_raw & 1)
        lon_value = (lon_raw >> 1) ^ -(lon_raw & 1)
        if have_coord:
            lat_i = prev_lat + lat_value
            lon_i = prev_lon + lon_value
        else:
            lat_i = lat_value
            lon_i = lon_value
        prev_lat, prev_lon = lat_i, lon_i
        have_coord = True
        out.append((node_id, lat_i / scale, lon_i / scale))
    return out





def encode_way_coordinates(
    nodes: Iterable[tuple[int, float | None, float | None]]
) -> bytes:
    """Encode the schema-v5 coordinate stream aligned with ``node_ids``.

    Each node contributes two varints (latitude, longitude). ``0,0`` marks a
    missing coordinate. Valid coordinates use 1e-7 degree integer precision
    and delta+zigzag encoding, with the previous valid coordinate as the base.
    """
    out = bytearray()
    prev_lat = 0
    prev_lon = 0
    have_coord = False
    for _node_id, lat, lon in nodes:
        if lat is None or lon is None:
            _put_uvarint(out, 0)
            _put_uvarint(out, 0)
            continue
        lat_i = _coord_to_int(lat)
        lon_i = _coord_to_int(lon)
        if have_coord:
            lat_value = lat_i - prev_lat
            lon_value = lon_i - prev_lon
        else:
            lat_value = lat_i
            lon_value = lon_i
        _put_uvarint(out, _zigzag_encode(lat_value) + 1)
        _put_uvarint(out, _zigzag_encode(lon_value) + 1)
        prev_lat, prev_lon = lat_i, lon_i
        have_coord = True
    return bytes(out)


def decode_way_coordinates(
    blob: bytes | memoryview | None,
) -> list[tuple[float | None, float | None]]:
    """Decode the aligned schema-v5 coordinate stream."""
    if blob is None:
        return []
    data = blob if isinstance(blob, bytes) else bytes(blob)
    n = len(data)
    if not n:
        return []

    out: list[tuple[float | None, float | None]] = []
    offset = 0
    prev_lat = 0
    prev_lon = 0
    have_coord = False
    scale = float(COORD_SCALE)

    while offset < n:
        lat_code = 0
        shift = 0
        while True:
            if offset >= n:
                raise ValueError("truncated latitude varint in schema-v5 coordinate blob")
            byte = data[offset]
            offset += 1
            lat_code |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid latitude varint in schema-v5 coordinate blob")

        lon_code = 0
        shift = 0
        while True:
            if offset >= n:
                raise ValueError("truncated longitude varint in schema-v5 coordinate blob")
            byte = data[offset]
            offset += 1
            lon_code |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid longitude varint in schema-v5 coordinate blob")

        if lat_code == 0 or lon_code == 0:
            if lat_code != 0 or lon_code != 0:
                raise ValueError("invalid partial coordinate in schema-v5 coordinate blob")
            out.append((None, None))
            continue

        lat_raw = lat_code - 1
        lon_raw = lon_code - 1
        lat_value = (lat_raw >> 1) ^ -(lat_raw & 1)
        lon_value = (lon_raw >> 1) ^ -(lon_raw & 1)
        if have_coord:
            lat_i = prev_lat + lat_value
            lon_i = prev_lon + lon_value
        else:
            lat_i = lat_value
            lon_i = lon_value
        prev_lat, prev_lon = lat_i, lon_i
        have_coord = True
        out.append((lat_i / scale, lon_i / scale))
    return out


def encode_way_geometry_v5(
    nodes: Iterable[tuple[int, float | None, float | None]]
) -> tuple[bytes, bytes]:
    """Encode schema-v5 split topology as aligned ID and coordinate streams."""
    values = list(nodes)
    return (
        encode_way_node_ids(node_id for node_id, _lat, _lon in values),
        encode_way_coordinates(values),
    )


def decode_way_geometry_v5(
    node_ids_blob: bytes | memoryview | None,
    coordinates_blob: bytes | memoryview | None,
) -> list[tuple[int, float | None, float | None]]:
    """Decode aligned schema-v5 ID and coordinate streams."""
    id_data = (
        b"" if node_ids_blob is None
        else node_ids_blob if isinstance(node_ids_blob, bytes)
        else bytes(node_ids_blob)
    )
    coord_data = (
        b"" if coordinates_blob is None
        else coordinates_blob if isinstance(coordinates_blob, bytes)
        else bytes(coordinates_blob)
    )
    id_n = len(id_data)
    coord_n = len(coord_data)
    if not id_n and not coord_n:
        return []
    if not id_n or not coord_n:
        raise ValueError("schema-v5 way stream length mismatch")

    out: list[tuple[int, float | None, float | None]] = []
    id_offset = 0
    coord_offset = 0
    prev_id: int | None = None
    prev_lat = 0
    prev_lon = 0
    have_coord = False
    scale = float(COORD_SCALE)

    while id_offset < id_n and coord_offset < coord_n:
        raw_id = 0
        shift = 0
        while True:
            if id_offset >= id_n:
                raise ValueError("truncated varint in way topology blob")
            byte = id_data[id_offset]
            id_offset += 1
            raw_id |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid varint in way topology blob")

        if prev_id is None:
            node_id = raw_id
        else:
            node_id = prev_id + ((raw_id >> 1) ^ -(raw_id & 1))
        prev_id = node_id

        lat_code = 0
        shift = 0
        while True:
            if coord_offset >= coord_n:
                raise ValueError("truncated latitude varint in schema-v5 coordinate blob")
            byte = coord_data[coord_offset]
            coord_offset += 1
            lat_code |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid latitude varint in schema-v5 coordinate blob")

        lon_code = 0
        shift = 0
        while True:
            if coord_offset >= coord_n:
                raise ValueError("truncated longitude varint in schema-v5 coordinate blob")
            byte = coord_data[coord_offset]
            coord_offset += 1
            lon_code |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid longitude varint in schema-v5 coordinate blob")

        if lat_code == 0 or lon_code == 0:
            if lat_code != 0 or lon_code != 0:
                raise ValueError("invalid partial coordinate in schema-v5 coordinate blob")
            out.append((int(node_id), None, None))
            continue

        lat_raw = lat_code - 1
        lon_raw = lon_code - 1
        lat_value = (lat_raw >> 1) ^ -(lat_raw & 1)
        lon_value = (lon_raw >> 1) ^ -(lon_raw & 1)
        if have_coord:
            lat_i = prev_lat + lat_value
            lon_i = prev_lon + lon_value
        else:
            lat_i = lat_value
            lon_i = lon_value
        prev_lat, prev_lon = lat_i, lon_i
        have_coord = True
        out.append((int(node_id), lat_i / scale, lon_i / scale))

    if id_offset != id_n or coord_offset != coord_n:
        raise ValueError("schema-v5 way stream length mismatch")
    return out


def decode_way_geometry_node_ids(blob: bytes | memoryview | None) -> list[int]:
    """Decode only ordered node IDs from a schema-v4 way blob.

    This is the hottest non-recursive hydration loop in large ``out center``
    queries. Coordinate values are not decoded at all: their varints are skipped
    by scanning to the terminating byte. Inlining the ID varint decoder avoids
    three Python function calls per node while preserving the exact node list.
    """
    if blob is None:
        return []
    data = blob if isinstance(blob, bytes) else bytes(blob)
    n = len(data)
    if not n:
        return []

    out: list[int] = []
    offset = 0
    prev_id: int | None = None
    while offset < n:
        raw_id = 0
        shift = 0
        while True:
            byte = data[offset]
            offset += 1
            raw_id |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 70:
                raise ValueError("invalid varint in way geometry blob")

        if prev_id is None:
            node_id = raw_id
        else:
            node_id = prev_id + ((raw_id >> 1) ^ -(raw_id & 1))
        prev_id = node_id
        out.append(node_id)

        # Skip latitude and longitude varints without reconstructing their
        # integer values. Bounds checks make malformed/truncated blobs fail in
        # the same class of way as the full decoder instead of reading past EOF.
        for _ in range(2):
            while True:
                if offset >= n:
                    raise ValueError("truncated varint in way geometry blob")
                byte = data[offset]
                offset += 1
                if byte < 0x80:
                    break
    return out

def insert_way_geometry_v5_rows(
    con: sqlite3.Connection,
    rows: Iterable[tuple[int, bytes, bytes]],
) -> None:
    con.executemany(
        "INSERT INTO way_geometry_v5(way_id,node_ids,coordinates) VALUES(?,?,?)",
        (
            (int(way_id), sqlite3.Binary(node_ids), sqlite3.Binary(coordinates))
            for way_id, node_ids, coordinates in rows
        ),
    )


def insert_way_geometry_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, bytes]]) -> None:
    con.executemany(
        "INSERT INTO way_geometry(way_id,node_data) VALUES(?,?)",
        ((int(way_id), sqlite3.Binary(blob)) for way_id, blob in rows),
    )


def get_way_nodes(con: sqlite3.Connection, way_id: int) -> list[tuple[int, float | None, float | None]]:
    row = con.execute(
        "SELECT node_ids,coordinates FROM way_geometry_v5 WHERE way_id=?", (int(way_id),)
    ).fetchone()
    if row is not None:
        return decode_way_geometry_v5(row["node_ids"], row["coordinates"])

    row = con.execute(
        "SELECT node_data FROM way_geometry WHERE way_id=?", (int(way_id),)
    ).fetchone()
    if row is not None:
        return decode_way_geometry(row["node_data"])

    # Schema-v3/v2 compatibility: recover coordinates from the global table.
    ids = get_way_node_ids(con, way_id)
    if not ids:
        return []
    coords: dict[int, tuple[float, float]] = {}
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = list(dict.fromkeys(ids[offset:offset + chunk_size]))
        placeholders = ",".join("?" for _ in chunk)
        for r in con.execute(
            f"SELECT node_id,lat,lon FROM node_coords WHERE node_id IN ({placeholders})",
            chunk,
        ):
            coords[int(r["node_id"])] = (float(r["lat"]), float(r["lon"]))
    return [(nid, *(coords.get(nid, (None, None)))) for nid in ids]


def get_way_nodes_by_way(
    con: sqlite3.Connection, way_ids: Iterable[int]
) -> dict[int, list[tuple[int, float | None, float | None]]]:
    """Batch-load ordered way nodes with coordinates across schema v2-v5."""
    ids = list(dict.fromkeys(int(v) for v in way_ids))
    out: dict[int, list[tuple[int, float | None, float | None]]] = {wid: [] for wid in ids}
    if not ids:
        return out
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        found: set[int] = set()
        for row in con.execute(
            f"SELECT way_id,node_ids,coordinates FROM way_geometry_v5 WHERE way_id IN ({placeholders})",
            chunk,
        ):
            wid = int(row["way_id"])
            found.add(wid)
            out[wid] = decode_way_geometry_v5(row["node_ids"], row["coordinates"])

        remaining_v4 = [wid for wid in chunk if wid not in found]
        if remaining_v4:
            rp = ",".join("?" for _ in remaining_v4)
            for row in con.execute(
                f"SELECT way_id,node_data FROM way_geometry WHERE way_id IN ({rp})",
                remaining_v4,
            ):
                wid = int(row["way_id"])
                found.add(wid)
                out[wid] = decode_way_geometry(row["node_data"])

        fallback = [wid for wid in chunk if wid not in found]
        if not fallback:
            continue
        topology = get_way_node_ids_by_way(con, fallback)
        unique_nodes: list[int] = []
        seen_nodes: set[int] = set()
        for wid in fallback:
            for nid in topology.get(wid, []):
                if nid not in seen_nodes:
                    seen_nodes.add(nid)
                    unique_nodes.append(nid)
        coords: dict[int, tuple[float, float]] = {}
        for pos in range(0, len(unique_nodes), chunk_size):
            node_chunk = unique_nodes[pos:pos + chunk_size]
            np = ",".join("?" for _ in node_chunk)
            for row in con.execute(
                f"SELECT node_id,lat,lon FROM node_coords WHERE node_id IN ({np})",
                node_chunk,
            ):
                coords[int(row["node_id"])] = (float(row["lat"]), float(row["lon"]))
        for wid in fallback:
            out[wid] = [
                (nid, *(coords.get(nid, (None, None))))
                for nid in topology.get(wid, [])
            ]
    return out


def get_way_nodes_many(
    con: sqlite3.Connection, way_ids: Iterable[int]
) -> dict[int, tuple[float | None, float | None]]:
    """Return unique node IDs for ways, retaining any available coordinate."""
    ids = [int(v) for v in way_ids]
    out: dict[int, tuple[float | None, float | None]] = {}
    if not ids:
        return out
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        found: set[int] = set()
        for row in con.execute(
            f"SELECT way_id,node_ids,coordinates FROM way_geometry_v5 WHERE way_id IN ({placeholders})",
            chunk,
        ):
            wid = int(row["way_id"])
            found.add(wid)
            for nid, lat, lon in decode_way_geometry_v5(row["node_ids"], row["coordinates"]):
                previous = out.get(nid)
                if previous is None or (previous[0] is None and lat is not None):
                    out[nid] = (lat, lon)

        remaining_v4 = [wid for wid in chunk if wid not in found]
        if remaining_v4:
            rp = ",".join("?" for _ in remaining_v4)
            for row in con.execute(
                f"SELECT way_id,node_data FROM way_geometry WHERE way_id IN ({rp})",
                remaining_v4,
            ):
                wid = int(row["way_id"])
                found.add(wid)
                for nid, lat, lon in decode_way_geometry(row["node_data"]):
                    previous = out.get(nid)
                    if previous is None or (previous[0] is None and lat is not None):
                        out[nid] = (lat, lon)

        fallback = [wid for wid in chunk if wid not in found]
        if fallback:
            fallback_ids = get_way_node_ids_many(con, fallback)
            if fallback_ids:
                node_list = list(fallback_ids)
                for pos in range(0, len(node_list), chunk_size):
                    node_chunk = node_list[pos:pos + chunk_size]
                    np = ",".join("?" for _ in node_chunk)
                    resolved = {
                        int(r["node_id"]): (float(r["lat"]), float(r["lon"]))
                        for r in con.execute(
                            f"SELECT node_id,lat,lon FROM node_coords WHERE node_id IN ({np})",
                            node_chunk,
                        )
                    }
                    for nid in node_chunk:
                        out.setdefault(nid, resolved.get(nid, (None, None)))
    return out


def insert_way_topology_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, bytes]]) -> None:
    con.executemany(
        "INSERT INTO way_topology(way_id,node_ids) VALUES(?,?)",
        ((int(way_id), sqlite3.Binary(blob)) for way_id, blob in rows),
    )


def get_way_node_ids(con: sqlite3.Connection, way_id: int) -> list[int]:
    row = con.execute(
        "SELECT node_ids FROM way_geometry_v5 WHERE way_id=?", (int(way_id),)
    ).fetchone()
    if row is not None:
        return decode_way_node_ids(row["node_ids"])
    row = con.execute(
        "SELECT node_data FROM way_geometry WHERE way_id=?", (int(way_id),)
    ).fetchone()
    if row is not None:
        return decode_way_geometry_node_ids(row["node_data"])
    row = con.execute(
        "SELECT node_ids FROM way_topology WHERE way_id=?", (int(way_id),)
    ).fetchone()
    if row is not None:
        return decode_way_node_ids(row["node_ids"])
    return [
        int(r["node_id"])
        for r in con.execute(
            "SELECT node_id FROM way_nodes WHERE way_id=? ORDER BY seq", (int(way_id),)
        )
    ]


def get_way_node_ids_many(con: sqlite3.Connection, way_ids: Iterable[int]) -> set[int]:
    ids = [int(v) for v in way_ids]
    out: set[int] = set()
    if not ids:
        return out
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        found: set[int] = set()
        for row in con.execute(
            f"SELECT way_id,node_ids FROM way_geometry_v5 WHERE way_id IN ({placeholders})",
            chunk,
        ):
            wid = int(row["way_id"])
            found.add(wid)
            out.update(decode_way_node_ids(row["node_ids"]))
        remaining_v4 = [wid for wid in chunk if wid not in found]
        if remaining_v4:
            rp = ",".join("?" for _ in remaining_v4)
            for row in con.execute(
                f"SELECT way_id,node_data FROM way_geometry WHERE way_id IN ({rp})",
                remaining_v4,
            ):
                wid = int(row["way_id"])
                found.add(wid)
                out.update(decode_way_geometry_node_ids(row["node_data"]))
        remaining = [wid for wid in chunk if wid not in found]
        if remaining:
            rp = ",".join("?" for _ in remaining)
            packed_found: set[int] = set()
            for row in con.execute(
                f"SELECT way_id,node_ids FROM way_topology WHERE way_id IN ({rp})",
                remaining,
            ):
                wid = int(row["way_id"])
                packed_found.add(wid)
                out.update(decode_way_node_ids(row["node_ids"]))
            legacy = [wid for wid in remaining if wid not in packed_found]
            if legacy:
                lp = ",".join("?" for _ in legacy)
                for row in con.execute(
                    f"SELECT node_id FROM way_nodes WHERE way_id IN ({lp})", legacy
                ):
                    out.add(int(row["node_id"]))
    return out


def insert_way_node_rows(con: sqlite3.Connection, rows: Iterable[tuple[int, int, int]]) -> None:
    con.executemany(
        "INSERT INTO way_nodes(way_id,node_id,seq) VALUES(?,?,?)",
        ((int(way_id), int(node_id), int(seq)) for way_id, node_id, seq in rows),
    )


def insert_relation_member_rows(
    con: sqlite3.Connection,
    relation_id: int,
    members: Iterable[tuple[str, int, str]],
) -> None:
    con.executemany(
        "INSERT INTO relation_members(relation_id,member_type,member_id,role,seq) VALUES(?,?,?,?,?)",
        ((int(relation_id), str(mtype), int(mid), str(role), seq)
         for seq, (mtype, mid, role) in enumerate(members)),
    )


def get_node_coord(con: sqlite3.Connection, node_id: int) -> tuple[float, float] | None:
    row = con.execute(
        "SELECT lat,lon FROM node_coords WHERE node_id=?", (int(node_id),)
    ).fetchone()
    if row is None:
        return None
    return float(row["lat"]), float(row["lon"])


def set_area_geometry(con: sqlite3.Connection, osm_type: str, osm_id: int, geojson: str) -> bool:
    row = con.execute(
        "SELECT pk FROM objects WHERE osm_type=? AND osm_id=?", (osm_type, int(osm_id))
    ).fetchone()
    if row is None:
        return False
    con.execute(
        "INSERT INTO area_geometries(object_pk,geojson) VALUES(?,?) "
        "ON CONFLICT(object_pk) DO UPDATE SET geojson=excluded.geojson",
        (int(row["pk"]), geojson),
    )
    return True


def get_area_geometry(con: sqlite3.Connection, object_pk: int) -> str | None:
    row = con.execute(
        "SELECT geojson FROM area_geometries WHERE object_pk=?", (int(object_pk),)
    ).fetchone()
    return None if row is None else str(row["geojson"])

def set_way_nodes(con: sqlite3.Connection, way_id: int, node_ids: Iterable[int]) -> None:
    con.execute("DELETE FROM way_nodes WHERE way_id=?", (way_id,))
    con.executemany(
        "INSERT INTO way_nodes(way_id,node_id,seq) VALUES(?,?,?)",
        ((way_id, int(node_id), seq) for seq, node_id in enumerate(node_ids)),
    )


def set_relation_members(con: sqlite3.Connection, relation_id: int, members: Iterable[tuple[str, int, str]]) -> None:
    con.execute("DELETE FROM relation_members WHERE relation_id=?", (relation_id,))
    con.executemany(
        "INSERT INTO relation_members(relation_id,member_type,member_id,role,seq) VALUES(?,?,?,?,?)",
        ((relation_id, str(mtype), int(mid), str(role), seq) for seq, (mtype, mid, role) in enumerate(members)),
    )



def get_tags_many(con: sqlite3.Connection, object_pks: Iterable[int]) -> dict[int, dict[str, str]]:
    """Batch-load tags for object primary keys."""
    pks = list(dict.fromkeys(int(v) for v in object_pks))
    out: dict[int, dict[str, str]] = {pk: {} for pk in pks}
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(pks), chunk_size):
        chunk = pks[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        for row in con.execute(
            f"SELECT object_pk,key,value FROM object_tags "
            f"WHERE object_pk IN ({placeholders}) ORDER BY object_pk",
            chunk,
        ):
            out[int(row["object_pk"])][str(row["key"])] = str(row["value"])
    return out

def get_way_node_ids_by_way(
    con: sqlite3.Connection, way_ids: Iterable[int]
) -> dict[int, list[int]]:
    """Batch-load ordered way topology across schema v2-v5.

    The return value is keyed by requested way ID. Missing topology is represented
    by an empty list, matching ``get_way_node_ids``.
    """
    ids = list(dict.fromkeys(int(v) for v in way_ids))
    out: dict[int, list[int]] = {wid: [] for wid in ids}
    if not ids:
        return out
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        found: set[int] = set()
        for row in con.execute(
            f"SELECT way_id,node_ids FROM way_geometry_v5 WHERE way_id IN ({placeholders})",
            chunk,
        ):
            wid = int(row["way_id"])
            found.add(wid)
            out[wid] = decode_way_node_ids(row["node_ids"])

        remaining_v4 = [wid for wid in chunk if wid not in found]
        if remaining_v4:
            rp = ",".join("?" for _ in remaining_v4)
            for row in con.execute(
                f"SELECT way_id,node_data FROM way_geometry WHERE way_id IN ({rp})",
                remaining_v4,
            ):
                wid = int(row["way_id"])
                found.add(wid)
                out[wid] = decode_way_geometry_node_ids(row["node_data"])

        remaining = [wid for wid in chunk if wid not in found]
        if remaining:
            rp = ",".join("?" for _ in remaining)
            packed_found: set[int] = set()
            for row in con.execute(
                f"SELECT way_id,node_ids FROM way_topology WHERE way_id IN ({rp})",
                remaining,
            ):
                wid = int(row["way_id"])
                packed_found.add(wid)
                out[wid] = decode_way_node_ids(row["node_ids"])

            legacy = [wid for wid in remaining if wid not in packed_found]
            if legacy:
                lp = ",".join("?" for _ in legacy)
                for row in con.execute(
                    f"SELECT way_id,node_id FROM way_nodes "
                    f"WHERE way_id IN ({lp}) ORDER BY way_id,seq",
                    legacy,
                ):
                    out[int(row["way_id"])].append(int(row["node_id"]))
    return out


def get_relation_members_many(
    con: sqlite3.Connection, relation_ids: Iterable[int]
) -> dict[int, list[dict]]:
    """Batch-load ordered relation members for API result hydration."""
    ids = list(dict.fromkeys(int(v) for v in relation_ids))
    out: dict[int, list[dict]] = {rid: [] for rid in ids}
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        for row in con.execute(
            f"SELECT relation_id,member_type,member_id,role FROM relation_members "
            f"WHERE relation_id IN ({placeholders}) ORDER BY relation_id,seq",
            chunk,
        ):
            out[int(row["relation_id"])].append({
                "type": str(row["member_type"]),
                "ref": int(row["member_id"]),
                "role": str(row["role"]),
            })
    return out

def get_tags(con: sqlite3.Connection, object_pk: int) -> dict[str, str]:
    return {
        row["key"]: row["value"]
        for row in con.execute(
            "SELECT key,value FROM object_tags WHERE object_pk=?", (object_pk,)
        )
    }


def seed_demo(con: sqlite3.Connection) -> None:
    """Insert synthetic objects used only for protocol/query tests.

    The coordinates are around Turku, Finland, but the IDs/data are deliberately
    synthetic and are not intended to represent real OSM objects.
    """
    demo = [
        OSMObject('node', 900000001, 60.4518, 22.2666, 60.4518, 60.4518, 22.2666, 22.2666,
                  {'amenity':'hospital','name':'Synthetic Turku Hospital'}),
        OSMObject('way', 900000002, 60.4521, 22.2670, 60.4518, 60.4524, 22.2667, 22.2673,
                  {'building':'hospital','name':'Synthetic Hospital Building'}, True),
        OSMObject('node', 900000003, 60.4522, 22.2671, 60.4522, 60.4522, 22.2671, 22.2671,
                  {'aeroway':'helipad','name':'Synthetic Hospital Helipad'}),
        OSMObject('way', 900000004, 60.4490, 22.2600, 60.4480, 60.4500, 22.2590, 22.2610,
                  {'tourism':'camp_site','name':'Synthetic Camp Site'}, True),
        OSMObject('node', 900000005, 60.4600, 22.2800, 60.4600, 60.4600, 22.2800, 22.2800,
                  {'natural':'peak','name':'Synthetic Peak'}),
        OSMObject('way', 900000006, 60.4550, 22.2550, 60.4540, 60.4560, 22.2540, 22.2560,
                  {'landuse':'farmland'}, True),
        OSMObject('way', 900000007, 60.4470, 22.2750, 60.4460, 60.4480, 22.2740, 22.2760,
                  {'landuse':'quarry'}, True),
        OSMObject('node', 900000008, 60.4530, 22.2500, 60.4530, 60.4530, 22.2500, 22.2500,
                  {'power':'transformer'}),
        OSMObject('way', 900000009, 60.4510, 22.2640, 60.4500, 60.4520, 22.2630, 22.2650,
                  {'highway':'primary'}),
        OSMObject('way', 900000010, 60.4510, 22.2640, 60.4500, 60.4520, 22.2630, 22.2650,
                  {'highway':'residential'}),
        OSMObject('node', 900000011, 60.4510, 22.2640, 60.4510, 60.4510, 22.2640, 22.2640, {}),
        OSMObject('way', 900000012, 60.4580, 22.2700, 60.4570, 60.4590, 22.2690, 22.2710,
                  {'aeroway':'aerodrome','name':'Synthetic Aerodrome'}, True),
        OSMObject('way', 900000013, 60.4430, 22.2820, 60.4420, 60.4440, 22.2810, 22.2830,
                  {'leisure':'pitch','sport':'soccer','surface':'grass','name':'Synthetic Grass Pitch'}, True),
        OSMObject('way', 900000014, 60.4420, 22.2860, 60.4410, 60.4430, 22.2850, 22.2870,
                  {'amenity':'parking','name':'Synthetic Parking'}, True),
        OSMObject('node', 900000015, 60.4425, 22.2865, 60.4425, 60.4425, 22.2865, 22.2865,
                  {'highway':'crossing','crossing':'uncontrolled'}),
        OSMObject('way', 900000016, 60.4400, 22.2900, 60.4390, 60.4410, 22.2890, 22.2910,
                  {'railway':'rail','name':'Synthetic Railway'}),
        OSMObject('node', 900000017, 60.4400, 22.2900, 60.4400, 60.4400, 22.2900, 22.2900, {}),
        OSMObject('node', 900000018, 60.4405, 22.2905, 60.4405, 60.4405, 22.2905, 22.2905, {}),
        OSMObject('way', 900000019, 60.4380, 22.2940, 60.4370, 60.4390, 22.2930, 22.2950,
                  {'natural':'water','water':'lake','name':'Synthetic Lake Outer'}, True),
        OSMObject('node', 900000020, 60.4375, 22.2935, 60.4375, 60.4375, 22.2935, 22.2935, {}),
        OSMObject('node', 900000021, 60.4385, 22.2945, 60.4385, 60.4385, 22.2945, 22.2945, {}),
        OSMObject('relation', 900000022, 60.4380, 22.2940, 60.4370, 60.4390, 22.2930, 22.2950,
                  {'natural':'water','water':'lake','type':'multipolygon','name':'Synthetic Lake Relation'}, True),
        OSMObject('node', 900000023, 60.4460, 22.2840, 60.4460, 60.4460, 22.2840, 22.2840,
                  {'emergency':'ambulance_station'}),
        OSMObject('way', 900000024, 60.4440, 22.2880, 60.4435, 60.4445, 22.2875, 22.2885,
                  {'building':'apartments','name':'Synthetic Apartments'}, True),
        OSMObject('relation', 900000025, 64.5, 26.0, 59.4, 70.3, 18.5, 32.0,
                  {'type':'boundary','boundary':'administrative','admin_level':'2',
                   'name':'Finland','name:en':'Finland','ISO3166-1':'FI',
                   'ISO3166-1:alpha2':'FI'}, True),
        OSMObject('node', 900000026, 60.2060, 25.1400, 60.2060, 60.2060, 25.1400, 25.1400,
                  {'natural':'cliff','name':'Synthetic Helsinki Cliff'}),
    ]
    for obj in demo:
        insert_object(con, obj)

    # Country is_in() tests require exact polygon containment. The synthetic
    # Finland relation therefore needs area geometry rather than relying on its
    # broad RTree bbox, which is only a candidate prefilter in production.
    if not set_area_geometry(
        con,
        'relation',
        900000025,
        json.dumps({
            'type': 'Polygon',
            'coordinates': [[
                [18.5, 59.4],
                [32.0, 59.4],
                [32.0, 70.3],
                [18.5, 70.3],
                [18.5, 59.4],
            ]],
        }, separators=(',', ':')),
    ):
        raise RuntimeError('failed to attach geometry to synthetic Finland relation')

    set_way_nodes(con, 900000009, [900000011])
    set_way_nodes(con, 900000010, [900000011])
    set_way_nodes(con, 900000016, [900000017, 900000018])
    set_way_nodes(con, 900000019, [900000020, 900000021])
    set_relation_members(con, 900000022, [('way', 900000019, 'outer')])
    con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('demo_seed','3')")
    con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('osm_timestamp','synthetic-demo')")
    con.commit()