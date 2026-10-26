from __future__ import annotations

import builtins
import json
import sqlite3
import time
from dataclasses import dataclass

from .db import OSMObject, encode_way_geometry_v5
from .import_profile import ANY_VALUE_KEYS, EXACT_VALUES
from .tier_c import queue_packed_tag_rows
from .bulk_write import WriteProfile, insert_object_rows, insert_geometry_rows


_BASE = None


@dataclass(slots=True)
class PreparedTagPayload:
    """Worker-prepared selector tags and packed full tags.

    Writers consume the fields directly; iteration and length queries decode
    the packed JSON on demand.
    """

    selector_items: tuple[tuple[str, str], ...]
    packed_blob: bytes | None

    def __iter__(self):
        if self.packed_blob is None:
            return iter(())
        payload = json.loads(self.packed_blob)
        return iter((str(k), str(v)) for k, v in payload.items())

    def __len__(self) -> int:
        if self.packed_blob is None:
            return 0
        payload = json.loads(self.packed_blob)
        return len(payload)


def _prepare_tag_payload(tags_items) -> PreparedTagPayload:
    """Prepare selector and hydration data from normalized worker tag tuples.

    ``_copy_way_record`` and the fast node/relation writers have already
    normalized these values to strings. Filtering and JSON encoding them here
    avoids recreating the tuple in both ``selector_tag_items`` and
    ``encode_packed_tags`` for every retained object.
    """
    items = tuple((str(k), str(v)) for k, v in tags_items)
    if not items:
        return PreparedTagPayload(selector_items=(), packed_blob=None)

    boundary_administrative = False
    admin_level_2 = False
    for key, value in items:
        if key == "boundary" and value == "administrative":
            boundary_administrative = True
        elif key == "admin_level" and value == "2":
            admin_level_2 = True

    if boundary_administrative and admin_level_2:
        selector_items = items
    else:
        selected: list[tuple[str, str]] = []
        for key, value in items:
            if key in ANY_VALUE_KEYS:
                selected.append((key, value))
                continue
            allowed = EXACT_VALUES.get(key)
            if allowed is not None and value in allowed:
                selected.append((key, value))
        selector_items = tuple(selected)

    packed_blob = json.dumps(
        {key: value for key, value in items},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return PreparedTagPayload(
        selector_items=selector_items,
        packed_blob=packed_blob,
    )


class FastImportObjectBatch:
    """Write schema-v6 objects with sequential primary keys and inline RTree rows.

    Packed full tags are spooled to a temporary file and loaded during finalization.
    """

    def __init__(
        self,
        con: sqlite3.Connection,
        object_limit: int = 25_000,
        tag_limit: int = 250_000,
        *,
        defer_rtree: bool = False,
        profile: WriteProfile | None = None,
    ):
        self.con = con
        self.profile = profile or WriteProfile()
        self.object_limit = max(1, int(object_limit))
        self.tag_limit = max(1, int(tag_limit))
        # Accept defer_rtree for compatibility; always update the RTree inline.
        self.defer_rtree = False
        row = con.execute("SELECT COALESCE(MAX(pk),0)+1 FROM objects").fetchone()
        self.next_pk = int(row[0])
        self.object_rows: list[tuple] = []
        self.rtree_rows: list[tuple] = []
        self.tag_rows: list[tuple] = []
        self.node_coord_rows: list[tuple[int, float, float]] = []
        self.packed_rows: list[tuple[int, bytes]] = []

    def _queue_tags(self, pk: int, tags_items) -> bool:
        payload = tags_items if isinstance(tags_items, PreparedTagPayload) else _prepare_tag_payload(tags_items)
        self.tag_rows.extend((pk, key, value) for key, value in payload.selector_items)
        if payload.packed_blob is not None:
            self.packed_rows.append((pk, payload.packed_blob))
            return True
        return False

    def _queue_object(
        self,
        osm_type: str,
        osm_id: int,
        center_lat: float,
        center_lon: float,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        area_eligible: bool,
        tags_items,
    ) -> int:
        pk = self.next_pk
        self.next_pk += 1
        has_tags = self._queue_tags(pk, tags_items)
        self.object_rows.append((
            pk, str(osm_type), int(osm_id), float(center_lat), float(center_lon),
            float(min_lat), float(max_lat), float(min_lon), float(max_lon),
            int(bool(area_eligible)), int(has_tags),
        ))
        self.rtree_rows.append((
            pk, float(min_lon), float(max_lon), float(min_lat), float(max_lat)
        ))
        if str(osm_type) == "node":
            self.node_coord_rows.append((int(osm_id), float(center_lat), float(center_lon)))
        if len(self.object_rows) >= self.object_limit or len(self.tag_rows) >= self.tag_limit:
            self.flush()
        return pk

    def add(self, obj: OSMObject) -> int:
        return self._queue_object(
            obj.osm_type, obj.osm_id, obj.center_lat, obj.center_lon,
            obj.min_lat, obj.max_lat, obj.min_lon, obj.max_lon,
            obj.area_eligible, obj.tags.items(),
        )

    def add_prepared(
        self,
        osm_type: str,
        osm_id: int,
        center_lat: float,
        center_lon: float,
        min_lat: float,
        max_lat: float,
        min_lon: float,
        max_lon: float,
        area_eligible: bool,
        tags_items,
    ) -> int:
        return self._queue_object(
            osm_type, osm_id, center_lat, center_lon,
            min_lat, max_lat, min_lon, max_lon,
            area_eligible, tags_items,
        )

    def flush(self) -> None:
        if self.object_rows:
            insert_object_rows(self.con, self.object_rows, profile=self.profile)
            self.object_rows.clear()
        auxiliary_started = time.perf_counter()
        if self.rtree_rows:
            self.con.executemany(
                "INSERT INTO object_rtree(pk,min_lon,max_lon,min_lat,max_lat) VALUES(?,?,?,?,?)",
                self.rtree_rows,
            )
            self.rtree_rows.clear()
        if self.tag_rows:
            self.con.executemany(
                "INSERT INTO object_tags(object_pk,key,value) VALUES(?,?,?)",
                self.tag_rows,
            )
            self.tag_rows.clear()
        if self.node_coord_rows:
            self.con.executemany(
                "INSERT OR IGNORE INTO node_coords(node_id,lat,lon) VALUES(?,?,?)",
                self.node_coord_rows,
            )
            self.node_coord_rows.clear()
        self.profile.auxiliary_sql_seconds += time.perf_counter() - auxiliary_started
        if self.packed_rows:
            spool_started = time.perf_counter()
            queue_packed_tag_rows(self.con, self.packed_rows)
            self.profile.spool_seconds += time.perf_counter() - spool_started
            self.packed_rows.clear()


def _prepare_way_batch_fast(records):
    """Parallel geometry + tag preparation, leaving SQLite work to one writer."""
    started = time.perf_counter()
    prepared = []
    missing_geometry = 0
    retained = 0
    dependencies = 0
    node_refs = 0

    for oid, nodes, tags_items, area_eligible, is_direct in records:
        min_lat = max_lat = min_lon = max_lon = None
        for _nid, lat, lon in nodes:
            if lat is None or lon is None:
                continue
            lat = float(lat)
            lon = float(lon)
            if min_lat is None:
                min_lat = max_lat = lat
                min_lon = max_lon = lon
            else:
                min_lat = min(min_lat, lat)
                max_lat = max(max_lat, lat)
                min_lon = min(min_lon, lon)
                max_lon = max(max_lon, lon)

        if min_lat is None:
            missing_geometry += 1
            continue

        center_lat = (min_lat + max_lat) / 2.0
        center_lon = (min_lon + max_lon) / 2.0
        node_ids_blob, coordinates_blob = encode_way_geometry_v5(nodes)
        tag_payload = _prepare_tag_payload(tags_items)
        prepared.append((
            int(oid), center_lat, center_lon,
            min_lat, max_lat, min_lon, max_lon,
            bool(area_eligible), tag_payload, node_ids_blob, coordinates_blob,
            bool(is_direct), len(nodes),
        ))
        node_refs += len(nodes)
        if is_direct:
            retained += 1
        else:
            dependencies += 1

    return (
        prepared,
        [],
        missing_geometry,
        retained,
        dependencies,
        node_refs,
        time.perf_counter() - started,
    )


def _discover_fast(osmium, pbf, con, stats, progress_every):
    base = _BASE
    way_ids = base.WayIdIndex(
        direct=osmium.index.IdSet(),
        wanted_tracker=osmium.IdTracker(),
    )
    pending_relations: dict[int, object] = {}
    needed_node_ids: set[int] = set()
    needed_relation_ids: set[int] = set()
    candidates = 0
    started = time.perf_counter()
    node_writer = FastImportObjectBatch(
        con,
        object_limit=base.TOPOLOGY_OBJECT_BATCH_SIZE,
        tag_limit=base.TOPOLOGY_TAG_BATCH_SIZE,
    )

    processor = osmium.FileProcessor(pbf).with_filter(
        osmium.filter.KeyFilter(*base.DISCOVERY_KEYS)
    )

    for obj in processor:
        candidates += 1
        stats.discovery_candidates += 1
        if progress_every and candidates % progress_every == 0:
            base._progress("discover", candidates, started)

        if obj.is_node():
            stats.discovery_candidate_nodes += 1
            if not base.is_relevant_items(obj.tags) or not obj.location.valid():
                continue
            tags = base._tags(obj)
            lat, lon = float(obj.location.lat), float(obj.location.lon)
            node_writer.add(OSMObject(
                "node", int(obj.id), lat, lon, lat, lat, lon, lon, tags, False
            ))
            stats.retained_tagged_nodes += 1

        elif obj.is_way():
            stats.discovery_candidate_ways += 1
            if base.is_relevant_items(obj.tags):
                way_ids.add_direct(int(obj.id))

        elif obj.is_relation():
            stats.discovery_candidate_relations += 1
            if not base.is_relevant_items(obj.tags):
                continue
            tags = base._tags(obj)
            members: list[tuple[str, int, str]] = []
            for member in obj.members:
                mtype = base._member_type(member)
                mid = int(member.ref)
                role = str(member.role or "")
                members.append((mtype, mid, role))
                if mtype == "way":
                    way_ids.add_dependency(mid)
                elif mtype == "node":
                    needed_node_ids.add(mid)
                elif mtype == "relation":
                    needed_relation_ids.add(mid)
            pending_relations[int(obj.id)] = base.PendingRelation(int(obj.id), tags, members)

    node_writer.flush()
    con.commit()
    elapsed = max(time.perf_counter() - started, 0.001)
    builtins.print(
        f"[discover] native prefilter complete: candidates={candidates:,} "
        f"elapsed={elapsed:,.1f}s rate={candidates/elapsed:,.0f}/s"
    )
    return way_ids, pending_relations, needed_node_ids, needed_relation_ids


def _ensure_osm_id_index(con: sqlite3.Connection) -> float:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_objects_osm_id'"
    ).fetchone()
    if exists is not None:
        return 0.0
    started = time.perf_counter()
    con.execute("CREATE INDEX idx_objects_osm_id ON objects(osm_id)")
    con.commit()
    elapsed = time.perf_counter() - started
    builtins.print(f"[identity-index] objects(osm_id) built in {elapsed:,.1f}s")
    return elapsed


def _member_bounds_fast(con, pending, rel_id, memo, visiting):
    base = _BASE
    if rel_id in memo:
        return memo[rel_id]
    if rel_id in visiting:
        return None
    visiting.add(rel_id)
    rel = pending.get(rel_id)
    if rel is None:
        visiting.remove(rel_id)
        return None

    boxes: list[tuple[float, float, float, float]] = []
    for mtype, mid, _role in rel.members:
        if mtype == "node":
            coord = base.get_node_coord(con, mid)
            if coord is not None:
                lat, lon = coord
                boxes.append((lat, lat, lon, lon))
        elif mtype == "way":
            row = con.execute(
                """SELECT min_lat,max_lat,min_lon,max_lon
                   FROM objects WHERE osm_id=? AND osm_type='way'""",
                (int(mid),),
            ).fetchone()
            if row is not None:
                boxes.append((float(row[0]), float(row[1]), float(row[2]), float(row[3])))
        elif mtype == "relation":
            child = _member_bounds_fast(con, pending, mid, memo, visiting)
            if child is not None:
                boxes.append(child)
    visiting.remove(rel_id)
    if not boxes:
        return None
    result = (
        min(b[0] for b in boxes), max(b[1] for b in boxes),
        min(b[2] for b in boxes), max(b[3] for b in boxes),
    )
    memo[rel_id] = result
    return result


def _flush_relation_members(con: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    con.executemany(
        "INSERT INTO relation_members(relation_id,member_type,member_id,role,seq) VALUES(?,?,?,?,?)",
        rows,
    )
    rows.clear()


def _finalize_relations_fast(con, pending, stats) -> None:
    # Build once after topology instead of maintaining this B-tree per imported
    # object. Relation bbox assembly and runtime identity lookup then stay fast.
    _ensure_osm_id_index(con)

    memo: dict[int, tuple[float, float, float, float]] = {}
    relation_writer = FastImportObjectBatch(con, object_limit=10_000, tag_limit=100_000)
    member_rows: list[tuple] = []
    area_rows: list[tuple[int, str]] = []

    for rid, rel in pending.items():
        bbox = _member_bounds_fast(con, pending, rid, memo, set())
        if bbox is None:
            stats.unresolved_relations += 1
            continue
        min_lat, max_lat, min_lon, max_lon = bbox
        tags = dict(rel.tags)
        country_geojson = tags.pop("__country_geojson", None)
        pk = relation_writer.add(OSMObject(
            "relation", rid,
            (min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0,
            min_lat, max_lat, min_lon, max_lon,
            tags, _BASE._relation_area_eligible(tags),
        ))
        member_rows.extend(
            (int(rid), str(mtype), int(mid), str(role), seq)
            for seq, (mtype, mid, role) in enumerate(rel.members)
        )
        if len(member_rows) >= 250_000:
            _flush_relation_members(con, member_rows)
        if country_geojson:
            area_rows.append((pk, str(country_geojson)))
            stats.country_geometries += 1
        stats.retained_relations += 1

    relation_writer.flush()
    _flush_relation_members(con, member_rows)
    if area_rows:
        con.executemany(
            """INSERT INTO area_geometries(object_pk,geojson) VALUES(?,?)
               ON CONFLICT(object_pk) DO UPDATE SET geojson=excluded.geojson""",
            area_rows,
        )
    con.commit()


def _verify_inline_rtree(con: sqlite3.Connection) -> int:
    object_count = int(con.execute("SELECT COUNT(*) FROM objects").fetchone()[0])
    rtree_count = int(con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0])
    if object_count != rtree_count:
        raise RuntimeError(
            f"inline object RTree row-count mismatch: objects={object_count:,} rtree={rtree_count:,}"
        )
    missing = con.execute(
        """SELECT o.pk FROM objects o
           LEFT JOIN object_rtree r ON r.pk=o.pk
           WHERE r.pk IS NULL LIMIT 1"""
    ).fetchone()
    if missing is not None:
        raise RuntimeError(f"inline object RTree missing pk={missing[0]}")
    return object_count


def _create_bulk_load_indexes_fast(con: sqlite3.Connection) -> None:
    rows = _verify_inline_rtree(con)
    builtins.print(f"[rtree] inline rows verified: {rows:,} [OK]")
    _ensure_osm_id_index(con)
    con.execute("CREATE INDEX IF NOT EXISTS idx_tags_key_value ON object_tags(key, value, object_pk)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tags_key ON object_tags(key, object_pk)")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_relation_members_member "
        "ON relation_members(member_type, member_id, relation_id)"
    )
    con.commit()


def install(base_module) -> None:
    """Bind the active import stages to the shared importer orchestration."""
    global _BASE
    _BASE = base_module
    base_module.ImportObjectBatch = FastImportObjectBatch
    base_module.insert_way_geometry_v5_rows = insert_geometry_rows
    base_module._prepare_way_batch = _prepare_way_batch_fast
    base_module._discover = _discover_fast
    base_module._member_bounds = _member_bounds_fast
    base_module._finalize_relations = _finalize_relations_fast
    base_module._create_bulk_load_indexes = _create_bulk_load_indexes_fast
