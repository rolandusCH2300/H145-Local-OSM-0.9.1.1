from __future__ import annotations

import argparse
import concurrent.futures as futures
import gc
import multiprocessing as mp
import queue
import threading
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .db import (
    OSMObject,
    connect,
    get_node_coord,
    insert_node_coords_ignore,
    encode_way_geometry_v5,
)
from .country_geometry import recover_selected_country_geometries
from .road_index import build_road_index
from .planner_stats import build_planner_stats
from .tag_spatial_index import build_tag_spatial_index
from .bulk_write import (
    DEFAULT_WRITE_OPTIONS, WriteProfile, add_build_write_arguments, discard_write_options,
    make_write_options, register_write_options,
)
from .import_optimizations import (
    PendingRelation,
    compact_int_ids,
    location_cache_used_memory,
    verify_foreign_keys,
)
from .import_profile import (
    DISCOVERY_KEYS,
    PROFILE_NAME,
    is_country_boundary,
    is_relevant_items,
)
from .tier_c import (
    SCHEMA_VERSION,
    discard_packed_tag_spool,
    finalize_tier_c_tags,
    prepare_tier_c_schema,
    verify_tier_c_identity,
)


TOPOLOGY_WORKER_BATCH_SIZE = 4_000
TOPOLOGY_WRITER_BATCH_SIZE = DEFAULT_WRITE_OPTIONS.writer_rows
TOPOLOGY_OBJECT_BATCH_SIZE = 25_000
TOPOLOGY_TAG_BATCH_SIZE = 250_000
SQLITE_BUILD_CACHE_MIB = 512
SQLITE_BUILD_CACHE_KIB = SQLITE_BUILD_CACHE_MIB * 1024
_SCHEDULED_NODE_CACHES: set[str] = set()


@dataclass(slots=True)
class WayIdIndex:
    """Compact exact way-ID membership backed by libosmium dense bitsets.

    ``direct`` distinguishes profile-matched ways from ways pulled in only as
    relation dependencies. ``wanted_tracker`` owns the union used by topology
    filtering, so topology does not materialize a second giant Python set or
    copy it through ``osmium.filter.IdFilter``.
    """

    direct: object
    wanted_tracker: object

    @property
    def wanted(self):
        return self.wanted_tracker.way_ids()

    def add_direct(self, osm_id: int) -> None:
        osm_id = int(osm_id)
        self.direct.set(osm_id)
        self.wanted_tracker.add_way(osm_id)

    def add_dependency(self, osm_id: int) -> None:
        self.wanted_tracker.add_way(int(osm_id))

    @property
    def dependency_only_count(self) -> int:
        return max(0, len(self.wanted) - len(self.direct))

    def clear(self) -> None:
        self.direct.clear()
        self.wanted.clear()


@dataclass(slots=True)
class ImportStats:
    # Fast mode intentionally pre-filters in native pyosmium before Python.
    # These are Python-visible candidate counts, not total source entity counts.
    discovery_candidates: int = 0
    discovery_candidate_nodes: int = 0
    discovery_candidate_ways: int = 0
    discovery_candidate_relations: int = 0
    topology_candidates: int = 0
    retained_tagged_nodes: int = 0
    retained_ways: int = 0
    retained_relations: int = 0
    dependency_node_refs: int = 0
    direct_dependency_nodes: int = 0
    standalone_node_coords: int = 0
    dependency_ways: int = 0
    way_geometry_rows: int = 0
    country_geometries: int = 0
    missing_way_geometry: int = 0
    unresolved_relations: int = 0
    missing_relation_dependencies: int = 0
    discovery_seconds: float = 0.0
    relation_dependency_seconds: float = 0.0
    topology_seconds: float = 0.0
    topology_worker_seconds: float = 0.0
    topology_writer_seconds: float = 0.0
    topology_wait_seconds: float = 0.0
    topology_future_wait_seconds: float = 0.0
    topology_enqueue_seconds: float = 0.0
    topology_drain_seconds: float = 0.0
    topology_write_profile: dict = field(default_factory=dict)
    topology_batches: int = 0
    topology_workers: int = 0
    sql_batch_rows: int = DEFAULT_WRITE_OPTIONS.sql_rows
    writer_batch_size: int = DEFAULT_WRITE_OPTIONS.writer_rows
    sql_write_mode: str = DEFAULT_WRITE_OPTIONS.sql_write_mode
    sqlite_cache_mib: int = 0
    node_cache_used_memory_bytes: int = 0
    finalize_seconds: float = 0.0
    index_build_seconds: float = 0.0
    road_index_rows: int = 0
    road_index_seconds: float = 0.0
    planner_stats_keys: int = 0
    planner_stats_values: int = 0
    planner_stats_seconds: float = 0.0
    tag_spatial_index_rows: int = 0
    tag_spatial_index_features: int = 0
    tag_spatial_index_seconds: float = 0.0
    packed_tags_rows: int = 0
    packed_tags_bytes: int = 0
    packed_tags_seconds: float = 0.0
    total_seconds: float = 0.0
    metadata_snapshot_seconds: float = 0.0
    final_verification_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    replacement_seconds: float = 0.0
    process_cpu_seconds: float = 0.0
    timing_scope: str = "metadata_snapshot"


def _drop_bulk_load_indexes(con: sqlite3.Connection) -> None:
    # Primary-key indexes remain. Only secondary indexes that are expensive to
    # maintain row-by-row are deferred until the immutable snapshot is loaded.
    con.execute("DROP INDEX IF EXISTS idx_objects_osm_id")
    con.execute("DROP INDEX IF EXISTS idx_tags_key_value")
    con.execute("DROP INDEX IF EXISTS idx_tags_key")
    con.execute("DROP INDEX IF EXISTS idx_relation_members_member")
    con.commit()


def _require_osmium():
    try:
        import osmium  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "The PBF importer requires pyosmium. Install it with:\n"
            "  py -m pip install osmium\n"
            "The local query server itself does not require pyosmium."
        ) from exc
    return osmium


def _tags(obj) -> dict[str, str]:
    return {str(k): str(v) for k, v in obj.tags}


def _member_type(member) -> str:
    value = str(member.type).lower()
    if value in {"n", "node"}:
        return "node"
    if value in {"w", "way"}:
        return "way"
    if value in {"r", "relation"}:
        return "relation"
    # pyosmium's relation member type normally stringifies to n/w/r.
    raise ValueError(f"unsupported relation member type: {member.type!r}")


def _bounds(coords: list[tuple[float, float]]) -> tuple[float, float, float, float, float, float]:
    lats = [p[0] for p in coords]
    lons = [p[1] for p in coords]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    # Representative centre used by R&E's out center results. Bbox midpoint is
    # deterministic and cheap; detailed topology is retained separately.
    return (
        (min_lat + max_lat) / 2.0,
        (min_lon + max_lon) / 2.0,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
    )


def _way_area_eligible(way, tags: dict[str, str]) -> bool:
    if tags.get("area") == "no":
        return False
    try:
        closed = bool(way.is_closed())
    except Exception:
        refs = [int(n.ref) for n in way.nodes]
        closed = len(refs) >= 4 and refs[0] == refs[-1]
    return closed


def _relation_area_eligible(tags: dict[str, str]) -> bool:
    return tags.get("type") in {"multipolygon", "boundary"} or tags.get("area") == "yes"


def _choose_location_cache(osmium, db_path: Path, requested: str | None) -> tuple[str, Path | None]:
    if requested:
        return requested, None
    available = set(osmium.index.map_types())
    if "sparse_file_array" in available:
        cache_path = db_path.with_suffix(db_path.suffix + ".nodecache")
        return f"sparse_file_array,{cache_path}", cache_path
    return "flex_mem", None


def _configure_build_sqlite_cache(con: sqlite3.Connection) -> int:
    """Apply and verify the importer-only SQLite page-cache budget."""
    con.execute(f"PRAGMA cache_size=-{SQLITE_BUILD_CACHE_KIB}")
    row = con.execute("PRAGMA cache_size").fetchone()
    effective_kib = int(row[0]) if row is not None else 0
    if effective_kib != -SQLITE_BUILD_CACHE_KIB:
        raise RuntimeError(
            "SQLite rejected the build page-cache setting: "
            f"requested=-{SQLITE_BUILD_CACHE_KIB} KiB effective={effective_kib}"
        )
    return SQLITE_BUILD_CACHE_MIB


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _progress(
    label: str,
    count: int,
    started: float,
    noun: str = "candidates",
    *,
    progress_count: int | None = None,
    progress_total: int | None = None,
    progress_noun: str = "ways",
) -> None:
    elapsed = max(time.perf_counter() - started, 0.001)
    suffix = ""
    if progress_count is not None and progress_total:
        pct = min(100.0, max(0.0, 100.0 * progress_count / progress_total))
        suffix = (
            f" progress={pct:5.1f}% "
            f"{progress_noun}={progress_count:,}/{progress_total:,}"
        )
    print(
        f"[{label}] {noun}={count:,} elapsed={elapsed:,.1f}s "
        f"rate={count/elapsed:,.0f}/s{suffix}"
    )


def _expand_relation_dependencies(
    osmium,
    pbf: Path,
    pending: dict[int, PendingRelation],
    way_ids: WayIdIndex,
    needed_node_ids: set[int],
    needed_relation_ids: set[int],
    stats: ImportStats,
    max_passes: int = 32,
) -> None:
    """Resolve nested relation members until closure or the pass limit."""
    unresolved = set(needed_relation_ids) - set(pending)
    pass_no = 0
    while unresolved and pass_no < max_passes:
        pass_no += 1
        print(f"[relations] resolving {len(unresolved):,} nested relation dependencies (pass {pass_no})")
        found: dict[int, PendingRelation] = {}
        processor = (
            osmium.FileProcessor(pbf, osmium.osm.RELATION)
            .with_filter(osmium.filter.IdFilter(unresolved))
        )
        for obj in processor:
            oid = int(obj.id)
            tags = _tags(obj)
            members: list[tuple[str, int, str]] = []
            for member in obj.members:
                mtype = _member_type(member)
                mid = int(member.ref)
                role = str(member.role or "")
                members.append((mtype, mid, role))
                if mtype == "way":
                    way_ids.add_dependency(mid)
                elif mtype == "node":
                    needed_node_ids.add(mid)
                elif mtype == "relation":
                    needed_relation_ids.add(mid)
            found[oid] = PendingRelation(oid, tags, members)

        if not found:
            break
        pending.update(found)
        unresolved = set(needed_relation_ids) - set(pending)

    unresolved = set(needed_relation_ids) - set(pending)
    stats.missing_relation_dependencies = len(unresolved)
    if unresolved:
        print(
            f"[relations] WARNING: {len(unresolved):,} referenced relations were not present "
            f"after {pass_no} dependency passes"
        )
    else:
        print(f"[relations] nested relation closure complete after {pass_no} pass(es)")


def _resolve_worker_count(value: str | int | None) -> int:
    """Resolve topology worker count; zero selects serial processing."""
    if value is None:
        value = "auto"
    if isinstance(value, int):
        return max(0, value)
    text = str(value).strip().lower()
    if text in {"0", "off", "none", "serial"}:
        return 0
    if text != "auto":
        try:
            return max(0, int(text))
        except ValueError as exc:
            raise ValueError("workers must be 'auto' or a non-negative integer") from exc
    logical = max(1, int(os.cpu_count() or 1))
    if logical <= 4:
        return 1
    # Keep the default conservative because pyosmium and SQLite also use threads.
    return max(2, min(8, logical // 2 - 1))


class _TopologyWriter:
    """Single SQLite writer running concurrently with PBF reading/workers."""

    def __init__(
        self,
        con: sqlite3.Connection,
        stats: ImportStats,
        queue_depth: int,
        writer_batch_size: int = TOPOLOGY_WRITER_BATCH_SIZE,
    ):
        self.con = con
        self.stats = stats
        self.queue: queue.Queue = queue.Queue(maxsize=max(2, int(queue_depth)))
        self.writer_batch_size = max(1, int(writer_batch_size))
        self.profile = WriteProfile()
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="re-local-osm-sqlite-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, result) -> float:
        """Submit prepared work and return time spent waiting for writer capacity."""
        started = time.perf_counter()
        while True:
            if self.error is not None:
                raise RuntimeError("topology SQLite writer failed") from self.error
            try:
                self.queue.put(result, timeout=0.10)
                return time.perf_counter() - started
            except queue.Full:
                continue

    def finish(self) -> None:
        while True:
            if self.error is not None:
                self._thread.join(timeout=0.1)
                raise RuntimeError("topology SQLite writer failed") from self.error
            try:
                self.queue.put(None, timeout=0.10)
                break
            except queue.Full:
                continue
        self._thread.join()
        if self.error is not None:
            raise RuntimeError("topology SQLite writer failed") from self.error

    def _run(self) -> None:
        active_seconds = 0.0
        cpu_started = time.thread_time()
        try:
            object_writer = ImportObjectBatch(self.con, defer_rtree=True, profile=self.profile)
            stop_after_batch = False
            while not stop_after_batch:
                first_result = self.queue.get()
                if first_result is None:
                    break

                results = [first_result]
                prepared_count = len(first_result[0])

                # Coalesce only work that is already queued. When the writer is
                # the bottleneck this rapidly builds ~64k-row SQLite batches;
                # when it is caught up, it writes the available batch immediately
                # instead of waiting for workers merely to hit the target size.
                while prepared_count < self.writer_batch_size:
                    try:
                        next_result = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if next_result is None:
                        stop_after_batch = True
                        break
                    results.append(next_result)
                    prepared_count += len(next_result[0])

                tick = time.perf_counter()
                geometry_rows: list[tuple[int, bytes, bytes]] = []
                for result in results:
                    prepared, _coord_rows, missing, retained, deps, node_refs, _worker_s = result
                    for (
                        oid, center_lat, center_lon,
                        min_lat, max_lat, min_lon, max_lon,
                        area_eligible, tags_items, node_ids_blob, coordinates_blob,
                        _is_direct, _ref_count,
                    ) in prepared:
                        object_writer.add_prepared(
                            "way", oid, center_lat, center_lon,
                            min_lat, max_lat, min_lon, max_lon,
                            area_eligible, tags_items,
                        )
                        geometry_rows.append((oid, node_ids_blob, coordinates_blob))
                    self.stats.missing_way_geometry += int(missing)
                    self.stats.retained_ways += int(retained)
                    self.stats.dependency_ways += int(deps)
                    self.stats.dependency_node_refs += int(node_refs)
                    # Preserve the historical meaning: one topology batch is one
                    # completed worker result, even when several are coalesced
                    # into a single SQLite writer batch.
                    self.stats.topology_batches += 1

                if geometry_rows:
                    insert_way_geometry_v5_rows(self.con, geometry_rows, profile=self.profile)
                active_seconds += time.perf_counter() - tick

            tick = time.perf_counter()
            object_writer.flush()
            commit_started = time.perf_counter()
            self.con.commit()
            self.profile.commit_seconds += time.perf_counter() - commit_started
            self.profile.transactions += 1
            self.profile.max_transaction_rows = self.profile.object_rows
            self.profile.max_transaction_bytes = 96 * self.profile.object_rows + self.profile.geometry_bytes
            active_seconds += time.perf_counter() - tick
        except BaseException as exc:  # propagate to main thread
            self.error = exc
        finally:
            self.stats.topology_writer_seconds += active_seconds
            self.profile.cpu_seconds += time.thread_time() - cpu_started
            self.profile.prepare_seconds = max(0.0, active_seconds - sum((
                self.profile.packing_seconds, self.profile.object_sql_seconds,
                self.profile.geometry_sql_seconds, self.profile.auxiliary_sql_seconds,
                self.profile.spool_seconds, self.profile.commit_seconds,
            )))
            self.stats.topology_write_profile = self.profile.as_dict()


def _copy_way_record(obj, direct_way_ids):
    oid = int(obj.id)
    nodes = []
    refs_for_closed: list[int] = []
    for n in obj.nodes:
        nid = int(n.ref)
        refs_for_closed.append(nid)
        if n.location.valid():
            nodes.append((nid, float(n.location.lat), float(n.location.lon)))
        else:
            nodes.append((nid, None, None))
    tags_items = tuple((str(k), str(v)) for k, v in obj.tags)
    area_no = any(k == "area" and v == "no" for k, v in tags_items)
    try:
        closed = bool(obj.is_closed())
    except Exception:
        closed = len(refs_for_closed) >= 4 and refs_for_closed[0] == refs_for_closed[-1]
    return (oid, tuple(nodes), tags_items, (not area_no and closed), oid in direct_way_ids)


def _build_topology_processor(osmium, pbf: Path, cache_spec: str, way_ids: WayIdIndex):
    area_boundary_filter = osmium.filter.TagFilter(("boundary", "administrative"))
    area_level_filter = osmium.filter.TagFilter(("admin_level", "2"))
    return (
        osmium.FileProcessor(pbf)
        .with_locations(cache_spec)
        .with_areas(area_boundary_filter, area_level_filter)
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY | osmium.osm.AREA))
        .with_filter(way_ids.wanted_tracker.id_filter().enable_for(osmium.osm.WAY))
        .with_filter(
            osmium.filter.TagFilter(("boundary", "administrative"))
            .enable_for(osmium.osm.AREA)
        )
        .with_filter(
            osmium.filter.TagFilter(("admin_level", "2"))
            .enable_for(osmium.osm.AREA)
        )
    )


def _finish_direct_dependency_nodes(processor, con, needed_node_ids, stats: ImportStats) -> None:
    location_store = processor.node_location_storage
    if location_store is not None and needed_node_ids:
        direct_rows: list[tuple[int, float, float]] = []
        for node_id in needed_node_ids:
            try:
                loc = location_store.get(int(node_id))
            except KeyError:
                continue
            if loc.valid():
                direct_rows.append((int(node_id), float(loc.lat), float(loc.lon)))
                if len(direct_rows) >= 100_000:
                    insert_node_coords_ignore(con, direct_rows)
                    stats.direct_dependency_nodes += len(direct_rows)
                    direct_rows.clear()
        if direct_rows:
            insert_node_coords_ignore(con, direct_rows)
            stats.direct_dependency_nodes += len(direct_rows)
        con.commit()


def _report_location_cache_memory(processor, stats: ImportStats) -> None:
    used = location_cache_used_memory(processor)
    if used is None:
        print("[topology] node-cache allocation: unavailable")
        return
    stats.node_cache_used_memory_bytes = max(stats.node_cache_used_memory_bytes, used)
    print(
        f"[topology] node-cache allocated={used / (1024**3):,.2f} GiB "
        f"({used:,} bytes)"
    )


def _import_topology_and_country_geometry_parallel(
    osmium,
    pbf: Path,
    con: sqlite3.Connection,
    way_ids: WayIdIndex,
    pending_relations: dict[int, PendingRelation],
    needed_node_ids,
    stats: ImportStats,
    cache_spec: str,
    progress_every: int,
    workers: int,
    worker_batch_size: int,
) -> None:
    country_relation_ids = {
        rid for rid, rel in pending_relations.items() if is_country_boundary(rel.tags)
    }
    geojson_factory = osmium.geom.GeoJSONFactory()
    processor = _build_topology_processor(osmium, pbf, cache_spec, way_ids)

    candidates = 0
    ways_processed = 0
    total_wanted_ways = len(way_ids.wanted)
    started = time.perf_counter()
    batch: list[tuple] = []
    max_inflight = max(2, workers * 2)
    pending_futures: set[futures.Future] = set()
    writer = _TopologyWriter(
        con,
        stats,
        queue_depth=max(2, workers * 2),
        writer_batch_size=stats.writer_batch_size,
    )
    writer.start()
    stats.topology_workers = workers
    print(f"[topology] wanted ways={total_wanted_ways:,}; progress percentage tracks selected ways")
    print(
        "[topology] batching: "
        f"worker={worker_batch_size:,} writer-target={stats.writer_batch_size:,} "
        f"sql-write-mode={stats.sql_write_mode} "
        f"sql-row-target={stats.sql_batch_rows:,} "
        f"objects={TOPOLOGY_OBJECT_BATCH_SIZE:,} tags={TOPOLOGY_TAG_BATCH_SIZE:,}"
    )

    def consume_done(done) -> None:
        for fut in done:
            result = fut.result()
            stats.topology_worker_seconds += float(result[-1])
            enqueue_seconds = writer.submit(result)
            stats.topology_enqueue_seconds += enqueue_seconds
            stats.topology_wait_seconds += enqueue_seconds

    spawn_ctx = mp.get_context("spawn")
    executor = futures.ProcessPoolExecutor(max_workers=workers, mp_context=spawn_ctx)
    pipeline_error: BaseException | None = None
    try:
        for obj in processor:
            candidates += 1
            stats.topology_candidates += 1
            is_way = obj.is_way()
            if is_way:
                ways_processed += 1
            if progress_every and candidates % progress_every == 0:
                _progress(
                    "topology", candidates, started,
                    progress_count=ways_processed,
                    progress_total=total_wanted_ways,
                )

            if is_way:
                batch.append(_copy_way_record(obj, way_ids.direct))
                if len(batch) >= worker_batch_size:
                    pending_futures.add(executor.submit(_prepare_way_batch, batch))
                    batch = []

                if len(pending_futures) >= max_inflight:
                    wait_started = time.perf_counter()
                    done, pending_futures = futures.wait(
                        pending_futures,
                        return_when=futures.FIRST_COMPLETED,
                    )
                    elapsed_wait = time.perf_counter() - wait_started
                    stats.topology_future_wait_seconds += elapsed_wait
                    stats.topology_wait_seconds += elapsed_wait
                    consume_done(done)

            elif obj.is_area() and not obj.from_way():
                rid = int(obj.orig_id())
                if rid not in country_relation_ids:
                    continue
                try:
                    geom = geojson_factory.create_multipolygon(obj)
                except Exception as exc:
                    print(f"[country] failed to assemble relation {rid}: {exc}")
                    continue
                rel = pending_relations.get(rid)
                if rel is not None:
                    rel.tags["__country_geojson"] = geom

        if batch:
            pending_futures.add(executor.submit(_prepare_way_batch, batch))
            batch = []

        while pending_futures:
            wait_started = time.perf_counter()
            done, pending_futures = futures.wait(
                pending_futures,
                return_when=futures.FIRST_COMPLETED,
            )
            elapsed_wait = time.perf_counter() - wait_started
            stats.topology_future_wait_seconds += elapsed_wait
            stats.topology_wait_seconds += elapsed_wait
            consume_done(done)
    except BaseException as exc:
        pipeline_error = exc
    finally:
        drain_started = time.perf_counter()
        executor.shutdown(wait=True, cancel_futures=True)
        try:
            writer.finish()
        except BaseException as writer_exc:
            if pipeline_error is None:
                pipeline_error = writer_exc
        stats.topology_drain_seconds = time.perf_counter() - drain_started

    if pipeline_error is not None:
        raise pipeline_error

    _finish_direct_dependency_nodes(processor, con, needed_node_ids, stats)
    _report_location_cache_memory(processor, stats)

    elapsed = max(time.perf_counter() - started, 0.001)
    pct = (100.0 * ways_processed / total_wanted_ways) if total_wanted_ways else 100.0
    print(
        f"[topology] parallel pipeline complete: candidates={candidates:,} "
        f"ways={ways_processed:,}/{total_wanted_ways:,} progress={min(100.0, pct):.1f}% "
        f"elapsed={elapsed:,.1f}s rate={candidates/elapsed:,.0f}/s workers={workers} "
        f"batches={stats.topology_batches:,}"
    )
    print(
        "[topology] profile: "
        f"worker-time(sum)={stats.topology_worker_seconds:,.1f}s, "
        f"sqlite-writer(active)={stats.topology_writer_seconds:,.1f}s, "
        f"backpressure/wait={stats.topology_wait_seconds:,.1f}s"
    )
    print(
        f"[topology] waits: worker-results={stats.topology_future_wait_seconds:,.1f}s "
        f"writer-enqueue={stats.topology_enqueue_seconds:,.1f}s "
        f"shutdown/drain={stats.topology_drain_seconds:,.1f}s"
    )
    print("[topology] writer detail (summed intervals): " + json.dumps(stats.topology_write_profile))

    processor = None
    gc.collect()


def _import_topology_and_country_geometry_serial(
    osmium,
    pbf: Path,
    con: sqlite3.Connection,
    way_ids: WayIdIndex,
    pending_relations: dict[int, PendingRelation],
    needed_node_ids,
    stats: ImportStats,
    cache_spec: str,
    progress_every: int,
) -> None:
    country_relation_ids = {
        rid for rid, rel in pending_relations.items() if is_country_boundary(rel.tags)
    }
    geojson_factory = osmium.geom.GeoJSONFactory()
    area_boundary_filter = osmium.filter.TagFilter(("boundary", "administrative"))
    area_level_filter = osmium.filter.TagFilter(("admin_level", "2"))

    # The location cache must still see every source node before filtering.
    # After that native filters keep only the exact wanted ways and country
    # boundary Areas out of Python. This preserves geometry completeness while
    # avoiding millions of irrelevant way/closed-way Area iterator objects.
    processor = (
        osmium.FileProcessor(pbf)
        .with_locations(cache_spec)
        .with_areas(area_boundary_filter, area_level_filter)
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY | osmium.osm.AREA))
        .with_filter(way_ids.wanted_tracker.id_filter().enable_for(osmium.osm.WAY))
        .with_filter(
            osmium.filter.TagFilter(("boundary", "administrative"))
            .enable_for(osmium.osm.AREA)
        )
        .with_filter(
            osmium.filter.TagFilter(("admin_level", "2"))
            .enable_for(osmium.osm.AREA)
        )
    )

    candidates = 0
    ways_processed = 0
    total_wanted_ways = len(way_ids.wanted)
    started = time.perf_counter()
    way_geometry_buffer: list[tuple[int, bytes, bytes]] = []
    object_writer = ImportObjectBatch(con, defer_rtree=True)
    print(f"[topology] wanted ways={total_wanted_ways:,}; progress percentage tracks selected ways")
    print(
        "[topology] batching: serial writer="
        f"{TOPOLOGY_WRITER_BATCH_SIZE:,} objects={TOPOLOGY_OBJECT_BATCH_SIZE:,} "
        f"tags={TOPOLOGY_TAG_BATCH_SIZE:,}"
    )

    def flush_buffers(*, commit: bool = False) -> None:
        object_writer.flush()
        if way_geometry_buffer:
            insert_way_geometry_v5_rows(con, way_geometry_buffer)
            way_geometry_buffer.clear()
        if commit:
            con.commit()

    for obj in processor:
        candidates += 1
        stats.topology_candidates += 1
        is_way = obj.is_way()
        if is_way:
            ways_processed += 1
        if progress_every and candidates % progress_every == 0:
            _progress(
                "topology", candidates, started,
                progress_count=ways_processed,
                progress_total=total_wanted_ways,
            )

        if is_way:
            oid = int(obj.id)
            if oid not in way_ids.wanted:
                continue

            coords: list[tuple[float, float]] = []
            refs: list[int] = []
            node_records: list[tuple[int, float | None, float | None]] = []
            for n in obj.nodes:
                nid = int(n.ref)
                refs.append(nid)
                if n.location.valid():
                    lat, lon = float(n.location.lat), float(n.location.lon)
                    coords.append((lat, lon))
                    node_records.append((nid, lat, lon))
                else:
                    node_records.append((nid, None, None))

            if not coords:
                stats.missing_way_geometry += 1
                continue

            node_ids_blob, coordinates_blob = encode_way_geometry_v5(node_records)
            way_geometry_buffer.append((oid, node_ids_blob, coordinates_blob))

            center_lat, center_lon, min_lat, max_lat, min_lon, max_lon = _bounds(coords)
            tags = _tags(obj)
            object_writer.add(OSMObject(
                "way", oid, center_lat, center_lon,
                min_lat, max_lat, min_lon, max_lon,
                tags, _way_area_eligible(obj, tags),
            ))
            if oid in way_ids.direct:
                stats.retained_ways += 1
            else:
                stats.dependency_ways += 1
            stats.dependency_node_refs += len(refs)

            if len(way_geometry_buffer) >= TOPOLOGY_WRITER_BATCH_SIZE:
                # Keep the topology phase in one staging transaction. With
                # journal_mode=OFF the build is disposable after interruption,
                # so repeated commits only add write/locking overhead.
                flush_buffers(commit=False)

        elif obj.is_area() and not obj.from_way():
            rid = int(obj.orig_id())
            if rid not in country_relation_ids:
                continue
            try:
                geom = geojson_factory.create_multipolygon(obj)
            except Exception as exc:
                print(f"[country] failed to assemble relation {rid}: {exc}")
                continue
            rel = pending_relations.get(rid)
            if rel is not None:
                rel.tags["__country_geojson"] = geom

    # Relation members can reference standalone nodes that are not part of a
    # retained way. The location cache has now seen the full node stream, so
    # fetch just those IDs directly instead of yielding every source node to
    # Python during the topology pass.
    location_store = processor.node_location_storage
    if location_store is not None and needed_node_ids:
        direct_rows: list[tuple[int, float, float]] = []
        for node_id in needed_node_ids:
            try:
                loc = location_store.get(int(node_id))
            except KeyError:
                continue
            if loc.valid():
                direct_rows.append((int(node_id), float(loc.lat), float(loc.lon)))
                if len(direct_rows) >= 100_000:
                    insert_node_coords_ignore(con, direct_rows)
                    stats.direct_dependency_nodes += len(direct_rows)
                    direct_rows.clear()
        if direct_rows:
            insert_node_coords_ignore(con, direct_rows)
            stats.direct_dependency_nodes += len(direct_rows)

    flush_buffers(commit=True)
    _report_location_cache_memory(processor, stats)
    elapsed = max(time.perf_counter() - started, 0.001)
    pct = (100.0 * ways_processed / total_wanted_ways) if total_wanted_ways else 100.0
    print(
        f"[topology] native iterator filter complete: candidates={candidates:,} "
        f"ways={ways_processed:,}/{total_wanted_ways:,} progress={min(100.0, pct):.1f}% "
        f"elapsed={elapsed:,.1f}s rate={candidates/elapsed:,.0f}/s"
    )

    # Release pyosmium's disk-backed location table before Windows cleanup.
    location_store = None
    processor = None
    gc.collect()


def _import_topology_and_country_geometry(
    osmium,
    pbf: Path,
    con: sqlite3.Connection,
    way_ids: WayIdIndex,
    pending_relations: dict[int, PendingRelation],
    needed_node_ids,
    stats: ImportStats,
    cache_spec: str,
    progress_every: int,
    workers: int,
    worker_batch_size: int,
) -> None:
    if workers <= 0:
        stats.topology_workers = 0
        return _import_topology_and_country_geometry_serial(
            osmium, pbf, con, way_ids, pending_relations,
            needed_node_ids, stats, cache_spec, progress_every
        )
    return _import_topology_and_country_geometry_parallel(
        osmium, pbf, con, way_ids, pending_relations,
        needed_node_ids, stats, cache_spec, progress_every, workers, worker_batch_size
    )


def _recover_selected_country_geometry_fallbacks(
    con: sqlite3.Connection,
    stats: ImportStats,
    selected_country_iso2,
):
    selected = {
        str(code).strip().upper()
        for code in (selected_country_iso2 or ())
        if str(code).strip()
    }
    if not selected:
        return []

    results = recover_selected_country_geometries(con, selected)
    recovered = [result for result in results if result.status == "recovered"]
    stats.country_geometries += len(recovered)
    for result in results:
        print(
            f"[country] fallback relation={result.relation_id} status={result.status} "
            f"outer={result.outer_rings} inner={result.inner_rings} "
            f"missing_ways={result.missing_way_members} "
            f"open_outer={result.skipped_open_outer_components}"
        )
    con.commit()
    return results


def _unlink_with_retries(path: Path, attempts: int = 8) -> bool:
    if not path.exists():
        return True
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            path.unlink()
            return True
        except OSError as exc:
            last_error = exc
            gc.collect()
            time.sleep(0.15 * (attempt + 1))
    if last_error is not None:
        print(f"[cleanup] could not remove {path}: {last_error}")
    return False


def _schedule_windows_delete_after_exit(path: Path) -> bool:
    """Schedule deletion after this Python process releases pyosmium's file handle.

    Some pyosmium/Windows combinations keep sparse_file_array open until
    interpreter shutdown. A detached helper waits for this process to exit,
    then removes the temporary cache.
    """
    if os.name != "nt" or not path.exists():
        return False
    resolved_path = str(path.resolve())
    if resolved_path in _SCHEDULED_NODE_CACHES:
        return True
    # sys.executable is the GUI executable in a frozen build, not a Python
    # interpreter accepting -c. Use the same external host in both runtimes.
    literal_path = resolved_path.replace("'", "''")
    helper = (
        f"$parentPid={os.getpid()}; $cachePath='{literal_path}'; "
        "Wait-Process -Id $parentPid -ErrorAction SilentlyContinue; "
        "for ($attempt=0; $attempt -lt 40; $attempt++) { "
        "if (-not (Test-Path -LiteralPath $cachePath)) { break }; "
        "try { Remove-Item -LiteralPath $cachePath -Force -ErrorAction Stop; break } "
        "catch { Start-Sleep -Milliseconds 250 } }"
    )
    # Combining DETACHED_PROCESS with CREATE_NO_WINDOW can make Windows
    # PowerShell exit without running -Command. Hidden stdio is sufficient;
    # Windows child processes can outlive their parent without DETACHED_PROCESS.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", helper],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=flags,
        )
        _SCHEDULED_NODE_CACHES.add(resolved_path)
        return True
    except OSError:
        return False


def _cleanup_node_cache(path: Path) -> None:
    try:
        path.unlink()
        return
    except FileNotFoundError:
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) in (32, 33) and _schedule_windows_delete_after_exit(path):
            print(f"[cleanup] node cache locked by pyosmium; scheduled deletion after process exit: {path}")
            return
    if not _unlink_with_retries(path):
        if _schedule_windows_delete_after_exit(path):
            print(f"[cleanup] scheduled node-cache deletion after process exit: {path}")


def import_pbf(
    pbf: Path,
    db_path: Path,
    *,
    replace: bool = False,
    location_cache: str | None = None,
    keep_cache: bool = False,
    progress_every: int = 500_000,
    workers: str | int | None = "auto",
    worker_batch_size: int = TOPOLOGY_WORKER_BATCH_SIZE,
    selected_country_iso2: tuple[str, ...] | list[str] | set[str] | None = None,
    sql_batch_rows: int = DEFAULT_WRITE_OPTIONS.sql_rows,
    writer_batch_size: int = TOPOLOGY_WRITER_BATCH_SIZE,
    sql_write_mode: str = DEFAULT_WRITE_OPTIONS.sql_write_mode,
) -> ImportStats:
    write_config = make_write_options(sql_batch_rows, writer_batch_size, sql_write_mode)
    osmium = _require_osmium()
    worker_count = _resolve_worker_count(workers)
    worker_batch_size = max(250, int(worker_batch_size))
    pbf = pbf.resolve()
    db_path = db_path.resolve()
    if not pbf.is_file():
        raise SystemExit(f"PBF not found: {pbf}")
    if db_path.exists() and not replace:
        raise SystemExit(f"Database already exists: {db_path}\nUse --replace to rebuild it safely.")

    # Always build alongside the target and swap only after verification. This
    # protects a currently-good country database from a failed/interrupted rebuild.
    build_path = db_path.with_name(db_path.name + ".building")
    for path in (build_path, Path(str(build_path) + "-wal"), Path(str(build_path) + "-shm")):
        if path.exists() and not _unlink_with_retries(path):
            raise SystemExit(f"Could not remove stale build file: {path}")

    cache_spec, cache_file = _choose_location_cache(osmium, build_path, location_cache)
    if cache_file and cache_file.exists() and not _unlink_with_retries(cache_file):
        raise SystemExit(f"Could not remove stale node cache: {cache_file}")

    print(f"H145 Local OSM importer profile: {PROFILE_NAME}")
    print(f"Source PBF: {pbf}")
    print(f"Target DB:  {db_path}")
    print(f"Build DB:   {build_path}")
    print(f"Location cache: {cache_spec}")
    print(
        "Topology processing: "
        + (f"multiprocessing workers={worker_count}, batch={worker_batch_size:,} ways"
           if worker_count > 0 else "serial")
    )
    if db_path.exists():
        print("Existing target will remain untouched until the new database passes verification.")

    con = connect(build_path)
    stats = ImportStats(
        sql_batch_rows=write_config.effective_sql_rows,
        writer_batch_size=write_config.writer_rows,
        sql_write_mode=write_config.sql_write_mode,
    )
    try:
        # This staging database can be recreated after interruption. Only a
        # verified build replaces the target; foreign keys are checked after
        # loading the authoritative rows.
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA foreign_keys=OFF")
        stats.sqlite_cache_mib = _configure_build_sqlite_cache(con)
        print(f"[sqlite] build page cache: {stats.sqlite_cache_mib:,} MiB")
        print(
            f"[sqlite] insert mode: {write_config.sql_write_mode}; "
            f"rows per SQL statement: {write_config.effective_sql_rows}"
        )
        prepare_tier_c_schema(con)
        print(
            f"[schema] Tier-C v{SCHEMA_VERSION}: compact sequential PKs, "
            "selector-only normalized tags, spooled full-tag hydration"
        )
        _drop_bulk_load_indexes(con)
        register_write_options(con, write_config)
        started = time.perf_counter()
        cpu_started = time.process_time()

        phase_started = time.perf_counter()
        way_ids, pending, needed_nodes, needed_relations = _discover(
            osmium, pbf, con, stats, progress_every
        )
        stats.discovery_seconds = time.perf_counter() - phase_started
        print(
            f"[discover] relevant ways={len(way_ids.direct):,} relations={len(pending):,} "
            f"relation-extra-ways={way_ids.dependency_only_count:,} "
            f"topology-wanted-ways={len(way_ids.wanted):,}"
        )
        phase_started = time.perf_counter()
        _expand_relation_dependencies(
            osmium, pbf, pending, way_ids, needed_nodes, needed_relations, stats
        )
        stats.relation_dependency_seconds = time.perf_counter() - phase_started
        needed_relations.clear()
        del needed_relations

        # Standalone relation-member node IDs are sparse and may have very high
        # numeric values. A dense bitset is therefore a poor fit. Keep a set only
        # while dependency closure needs deduplication, then compact to 8 bytes/ID
        # before topology and release the Python hash table.
        compact_needed_nodes = compact_int_ids(needed_nodes)
        needed_nodes.clear()
        del needed_nodes
        needed_nodes = compact_needed_nodes
        print(
            f"[relations] compact standalone-node IDs={len(needed_nodes):,} "
            f"payload={len(needed_nodes) * needed_nodes.itemsize / (1024**2):,.1f} MiB"
        )
        gc.collect()

        phase_started = time.perf_counter()
        _import_topology_and_country_geometry(
            osmium, pbf, con, way_ids, pending, needed_nodes,
            stats, cache_spec, progress_every, worker_count, worker_batch_size
        )
        stats.topology_seconds = time.perf_counter() - phase_started

        # Topology is the last consumer of the large exact-ID indexes and direct
        # relation-node dependency set. Drop them before relation finalization and
        # index construction so their memory cannot accumulate into later phases.
        way_ids.clear()
        del way_ids, needed_nodes
        gc.collect()

        phase_started = time.perf_counter()
        _finalize_relations(con, pending, stats)
        pending.clear()
        del pending
        gc.collect()
        _recover_selected_country_geometry_fallbacks(
            con,
            stats,
            selected_country_iso2,
        )
        stats.finalize_seconds = time.perf_counter() - phase_started
        stats.standalone_node_coords = int(
            con.execute("SELECT COUNT(*) FROM node_coords").fetchone()[0]
        )
        stats.way_geometry_rows = int(
            con.execute("SELECT COUNT(*) FROM way_geometry_v5").fetchone()[0]
        )

        phase_started = time.perf_counter()
        print("[index] verifying inline object RTree and building secondary indexes...")
        _create_bulk_load_indexes(con)
        stats.index_build_seconds = time.perf_counter() - phase_started
        print(f"[index] complete in {stats.index_build_seconds:,.1f}s")

        # Foreign keys and unique OSM identities are acceptance gates before
        # derived accelerators are built.
        verify_foreign_keys(con)
        print("[verify] SQLite foreign_key_check: ok")
        verify_tier_c_identity(con)
        print("[verify] unique OSM object identity: ok")

        # Load the spooled full tags, verify one packed row per tagged object,
        # and publish the metadata used by the runtime packed-tag loader.
        (
            stats.packed_tags_rows,
            stats.packed_tags_bytes,
            stats.packed_tags_seconds,
        ) = finalize_tier_c_tags(con)
        selector_rows = int(con.execute("SELECT COUNT(*) FROM object_tags").fetchone()[0])
        print(
            f"[packed-tags] spooled bulk-load: rows={stats.packed_tags_rows:,} "
            f"payload={stats.packed_tags_bytes / (1024**2):,.1f} MiB "
            f"selector-rows={selector_rows:,} verify={stats.packed_tags_seconds:,.1f}s"
        )

        # Derived, optional runtime acceleration structures. They duplicate no
        # authoritative OSM geometry/tags and can be rebuilt later without PBF.
        # Keep each derived builder at a clean transaction boundary. Some of
        # these builders use their own BEGIN IMMEDIATE transaction; an earlier
        # builder must never leave an implicit sqlite3 transaction open.
        con.commit()
        stats.road_index_rows, stats.road_index_seconds = build_road_index(con, verbose=True)
        con.commit()
        (
            stats.planner_stats_keys,
            stats.planner_stats_values,
            stats.planner_stats_seconds,
        ) = build_planner_stats(con, verbose=True)
        con.commit()
        (
            stats.tag_spatial_index_rows,
            tag_spatial_counts,
            stats.tag_spatial_index_seconds,
        ) = build_tag_spatial_index(con, verbose=True)
        con.commit()
        stats.tag_spatial_index_features = len(tag_spatial_counts)

        _set_meta(con, "profile", PROFILE_NAME)
        _set_meta(con, "source_pbf", str(pbf))
        _set_meta(con, "source_size_bytes", str(pbf.stat().st_size))
        _set_meta(con, "imported_at", datetime.now(timezone.utc).isoformat())
        _set_meta(con, "schema_version", str(SCHEMA_VERSION))
        stats.metadata_snapshot_seconds = time.perf_counter() - started
        stats.total_seconds = stats.metadata_snapshot_seconds
        stats.process_cpu_seconds = time.process_time() - cpu_started
        _set_meta(con, "import_stats", json.dumps({
            name: getattr(stats, name) for name in stats.__dataclass_fields__
        }, sort_keys=True))
        _set_meta(con, "osm_timestamp", datetime.fromtimestamp(pbf.stat().st_mtime, timezone.utc).isoformat())
        verification_started = time.perf_counter()
        con.execute("PRAGMA optimize")
        con.commit()
        check = con.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {check[0] if check else 'no result'}")
        print("[verify] SQLite quick_check: ok")
        stats.final_verification_seconds = time.perf_counter() - verification_started
    finally:
        cleanup_started = time.perf_counter()
        try:
            discard_packed_tag_spool(con)
        finally:
            discard_write_options(con)
            con.close()
        gc.collect()
        if cache_file and cache_file.exists() and not keep_cache:
            _cleanup_node_cache(cache_file)
        stats.cleanup_seconds = time.perf_counter() - cleanup_started

    # journal_mode was switched from WAL to OFF after schema creation; clean up
    # any staging sidecars that a particular SQLite build may have left behind.
    _unlink_with_retries(Path(str(build_path) + "-wal"))
    _unlink_with_retries(Path(str(build_path) + "-shm"))

    # A cleanly closed target should not have WAL sidecars. Refuse to replace
    # when they are present rather than risking an active server connection.
    if db_path.exists():
        live_sidecars = [
            side for side in (Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))
            if side.exists()
        ]
        if live_sidecars:
            raise SystemExit(
                "New database is verified but target appears to be in use (WAL sidecars exist).\n"
                "Stop the local OSM server and rerun the final replacement, or import to a new filename.\n"
                f"Verified build remains at: {build_path}"
            )

    replacement_started = time.perf_counter()
    try:
        os.replace(build_path, db_path)
    except OSError as exc:
        raise SystemExit(
            f"New database is verified but could not replace target: {exc}\n"
            f"Verified build remains at: {build_path}"
        ) from exc

    elapsed = time.perf_counter() - started
    stats.replacement_seconds = time.perf_counter() - replacement_started
    stats.total_seconds = elapsed
    stats.process_cpu_seconds = time.process_time() - cpu_started
    stats.timing_scope = "through_final_replace"
    print(f"Import complete in {elapsed/60.0:,.1f} min")
    print(
        "Phase times: "
        f"discover={stats.discovery_seconds:,.1f}s, "
        f"relation-deps={stats.relation_dependency_seconds:,.1f}s, "
        f"topology={stats.topology_seconds:,.1f}s, "
        f"finalize={stats.finalize_seconds:,.1f}s, "
        f"indexes={stats.index_build_seconds:,.1f}s, "
        f"road-index={stats.road_index_seconds:,.1f}s, planner-stats={stats.planner_stats_seconds:,.1f}s, "
        f"tag-spatial={stats.tag_spatial_index_seconds:,.1f}s"
    )
    print(f"Database size: {db_path.stat().st_size / (1024**3):.2f} GiB")
    print(
        f"[timing] metadata-snapshot={stats.metadata_snapshot_seconds:,.1f}s "
        f"final-verify={stats.final_verification_seconds:,.1f}s "
        f"close/cache-cleanup={stats.cleanup_seconds:,.1f}s "
        f"replacement={stats.replacement_seconds:,.3f}s total={stats.total_seconds:,.1f}s"
    )
    print(json.dumps({name: getattr(stats, name) for name in stats.__dataclass_fields__}, indent=2))
    return stats


def main() -> None:
    p = argparse.ArgumentParser(description="Import a Geofabrik .osm.pbf into the H145 Local OSM SQLite database")
    p.add_argument("--pbf", type=Path, required=True, help="input .osm.pbf file")
    p.add_argument("--db", type=Path, required=True, help="output SQLite database")
    p.add_argument("--replace", action="store_true", help="replace an existing database")
    p.add_argument("--location-cache", help="pyosmium location storage, e.g. flex_mem or sparse_file_array,C:\\temp\\nodes.idx")
    p.add_argument("--keep-cache", action="store_true", help="keep the temporary disk node cache")
    p.add_argument("--progress-every", type=int, default=500_000)
    p.add_argument(
        "--workers", default="auto",
        help="topology worker processes: auto (default, up to 8), 0 for serial, or an integer",
    )
    p.add_argument(
        "--worker-batch-size", type=int, default=TOPOLOGY_WORKER_BATCH_SIZE,
        help=f"ways per multiprocessing work batch (default: {TOPOLOGY_WORKER_BATCH_SIZE})",
    )
    add_build_write_arguments(p)
    args = p.parse_args()
    import_pbf(
        args.pbf, args.db,
        replace=args.replace,
        location_cache=args.location_cache,
        keep_cache=args.keep_cache,
        progress_every=max(0, args.progress_every),
        workers=args.workers,
        worker_batch_size=max(250, args.worker_batch_size),
        sql_batch_rows=args.sql_batch_rows,
        writer_batch_size=args.writer_batch_size,
        sql_write_mode=args.sql_write_mode,
    )


if __name__ == "__main__":
    main()
