from __future__ import annotations

import argparse
import json
import queue
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .coverage import CoverageGuard, CoverageMiss
from .db import connect, open_existing_query_db, seed_demo
from .query import QueryProfile, UnsupportedQuery, execute_overpass_subset
from .overpass_wire import canonicalize_elements
from .road_index import inspect_road_index
from .planner_stats import inspect_planner_stats
from .tag_spatial_index import inspect_tag_spatial_index
from .hydration_cache import inspect_hydration_cache

COPYRIGHT = "© OpenStreetMap contributors, ODbL 1.0"
_CONSOLE_LOG_LOCK = threading.Lock()


def _emit_console(kind: str, obj: dict) -> None:
    """Write one atomic, human-readable console event.

    ThreadingHTTPServer may finish several requests at nearly the same time.
    Building the whole block first and printing it under one lock prevents
    pretty-printed JSON from different requests being interleaved.
    """
    block = f"[H145 Local OSM] {kind}\n{json.dumps(obj, indent=2, ensure_ascii=False)}"
    with _CONSOLE_LOG_LOCK:
        print(block, flush=True)


def _useful_timing_ms(profile: QueryProfile) -> dict[str, float]:
    """Return only timing buckets large enough to be useful in console output."""
    timings = (
        ("sql", profile.candidate_sql_ms + profile.is_in_sql_ms),
        ("hydrate", profile.hydrate_ms + profile.is_in_hydrate_ms),
        ("recurse", profile.recurse_ms),
        (
            "road",
            profile.road_topology_ms + profile.road_hydrate_ms + profile.road_output_ms,
        ),
        ("is_in_geometry", profile.is_in_geometry_ms),
        ("output", profile.output_ms + profile.is_in_output_ms),
        ("filter", profile.candidate_filter_ms),
        ("coverage", profile.coverage_prepare_ms + profile.coverage_filter_ms),
        ("other", profile.unprofiled_ms),
    )
    result: dict[str, float] = {}
    for name, value in timings:
        rounded = round(float(value), 2)
        if rounded >= 0.10:
            result[name] = rounded
    return result


def _query_path(profile: QueryProfile) -> list[str]:
    path: list[str] = []
    if profile.adaptive_tag_first_calls:
        path.append("tag_first")
    if profile.adaptive_spatial_first_calls:
        path.append("spatial_first")
    if profile.road_index_calls:
        path.append("road_index")
    if profile.tag_spatial_index_calls:
        path.append("tag_spatial_index")
    if profile.packed_tags_calls:
        path.append("packed_tags")
    if profile.packed_way_ids_calls:
        path.append("packed_way_ids")
    return path


class State:
    def __init__(self, db_path: Path, db_readers: int = 1):
        self.db_path = db_path
        if not (1 <= int(db_readers) <= 8):
            raise ValueError("db_readers must be between 1 and 8")
        self.db_readers = int(db_readers)

        # Each reader owns its own persistent query-only SQLite connection.
        # SQLite permits concurrent read transactions, while the bounded pool
        # prevents an R&E burst from creating an unbounded number of readers.
        self.cons = [open_existing_query_db(db_path) for _ in range(self.db_readers)]
        self.coverage_guard = CoverageGuard.from_connection(self.cons[0])
        self._available: queue.LifoQueue[tuple[int, sqlite3.Connection]] = queue.LifoQueue(
            maxsize=self.db_readers
        )
        for slot, con in reversed(list(enumerate(self.cons))):
            self._available.put((slot, con))

        # HTTP-level query activity counters. These deliberately count requests
        # before they wait for a DB reader so we can distinguish a truly
        # concurrent R&E client from a sequential request/response client.
        self._activity_lock = threading.Lock()
        self._next_request_id = 1
        self._active_query_requests = 0
        self._max_active_query_requests_seen = 0
        self._total_query_requests = 0

    def begin_query_request(self) -> tuple[int, int, int, int]:
        with self._activity_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            self._active_query_requests += 1
            self._total_query_requests += 1
            if self._active_query_requests > self._max_active_query_requests_seen:
                self._max_active_query_requests_seen = self._active_query_requests
            return (
                request_id,
                self._active_query_requests,
                self._max_active_query_requests_seen,
                self._total_query_requests,
            )

    def end_query_request(self) -> tuple[int, int, int]:
        with self._activity_lock:
            self._active_query_requests = max(0, self._active_query_requests - 1)
            return (
                self._active_query_requests,
                self._max_active_query_requests_seen,
                self._total_query_requests,
            )

    def query_activity(self) -> dict[str, int]:
        with self._activity_lock:
            return {
                "active_query_requests": self._active_query_requests,
                "max_active_query_requests_seen": self._max_active_query_requests_seen,
                "total_query_requests": self._total_query_requests,
            }

    def acquire(self) -> tuple[int, sqlite3.Connection]:
        return self._available.get()

    def release(self, slot: int, con: sqlite3.Connection) -> None:
        self._available.put((slot, con))

    def close(self) -> None:
        for con in self.cons:
            con.close()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "H145LocalOSM/0.6"

        def log_message(self, fmt: str, *args) -> None:
            # Successful API requests are logged once, below, with decoded query
            # text and useful timings. Routine health/access lines add noise.
            return None

        def _cors(self) -> None:
            # H145's tablet/runtime may originate from an embedded browser context.
            # The listener itself is loopback-only; CORS does not make it network-accessible.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, status: int, obj: dict) -> None:
            payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as exc:
                _emit_console(
                    "WARNING",
                    {
                        "event": "client_disconnected",
                        "path": urllib.parse.urlsplit(self.path).path,
                        "detail": str(exc),
                    },
                )

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/health":
                slot, con = state.acquire()
                try:
                    ts = con.execute(
                        "SELECT value FROM metadata WHERE key='osm_timestamp'"
                    ).fetchone()
                    count = con.execute("SELECT COUNT(*) AS n FROM objects").fetchone()["n"]
                    standalone_nodes = con.execute(
                        "SELECT COUNT(*) AS n FROM node_coords"
                    ).fetchone()["n"]
                    schema_row = con.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                    try:
                        schema_version = int(schema_row["value"]) if schema_row is not None else 0
                    except (TypeError, ValueError):
                        schema_version = 0

                    if schema_version >= 5:
                        topology_ways = con.execute(
                            "SELECT COUNT(*) AS n FROM way_geometry_v5"
                        ).fetchone()["n"]
                    elif schema_version >= 4:
                        topology_ways = con.execute(
                            "SELECT COUNT(*) AS n FROM way_geometry"
                        ).fetchone()["n"]
                    elif schema_version >= 3:
                        topology_ways = con.execute(
                            "SELECT COUNT(*) AS n FROM way_topology"
                        ).fetchone()["n"]
                    else:
                        # Demo/legacy DBs may not carry schema metadata. Prefer
                        # the first populated representation because connect()
                        # creates compatibility tables even on older databases.
                        topology_ways = con.execute(
                            "SELECT COUNT(*) AS n FROM way_geometry"
                        ).fetchone()["n"]
                        if not topology_ways:
                            topology_ways = con.execute(
                                "SELECT COUNT(*) AS n FROM way_topology"
                            ).fetchone()["n"]
                        if not topology_ways:
                            topology_ways = con.execute(
                                "SELECT COUNT(DISTINCT way_id) AS n FROM way_nodes"
                            ).fetchone()["n"]

                    topology_refs = None
                    stats_row = con.execute(
                        "SELECT value FROM metadata WHERE key='import_stats'"
                    ).fetchone()
                    if stats_row is not None:
                        try:
                            topology_refs = json.loads(stats_row["value"]).get("dependency_node_refs")
                        except Exception:
                            topology_refs = None
                    profile_row = con.execute(
                        "SELECT value FROM metadata WHERE key='profile'"
                    ).fetchone()
                    road_info = inspect_road_index(con)
                    planner_info = inspect_planner_stats(con)
                    tag_spatial_info = inspect_tag_spatial_index(con)
                    hydration_info = inspect_hydration_cache(con)
                    activity_info = state.query_activity()
                finally:
                    state.release(slot, con)

                return self._json(
                    200,
                    {
                        "status": "ready",
                        "service": "H145 Local OSM",
                        "db_readers": state.db_readers,
                        **activity_info,
                        "schema_version": schema_version,
                        "objects": count,
                        # Backward-compatible field name; schemas 4/5 keep only
                        # independently addressable nodes in node_coords.
                        "topology_nodes": standalone_nodes,
                        "standalone_node_coords": standalone_nodes,
                        "topology_ways": topology_ways,
                        "topology_node_refs": topology_refs,
                        "road_index_ready": bool(road_info["ready"]),
                        "road_index_rows": int(road_info["rows"]),
                        "road_index_version": int(road_info["version"]),
                        "road_index_fast_nodes_ready": bool(road_info["fast_nodes_ready"]),
                        "road_index_fast_node_rows": int(road_info["fast_node_rows"]),
                        "road_index_fast_node_bytes": int(road_info["fast_node_bytes"]),
                        "planner_stats_ready": bool(planner_info["ready"]),
                        "planner_stats_keys": int(planner_info["keys"]),
                        "planner_stats_values": int(planner_info["values"]),
                        "tag_spatial_index_ready": bool(tag_spatial_info["ready"]),
                        "tag_spatial_index_features": int(tag_spatial_info["features"]),
                        "tag_spatial_index_rows": int(tag_spatial_info["rows"]),
                        "packed_tags_ready": bool(hydration_info["packed_tags_ready"]),
                        "packed_tags_rows": int(hydration_info["packed_tags_rows"]),
                        "packed_way_ids_ready": bool(hydration_info["packed_way_ids_ready"]),
                        "packed_way_ids_rows": int(hydration_info["packed_way_ids_rows"]),
                        "profile": profile_row["value"] if profile_row else None,
                        "osm_timestamp": ts["value"] if ts else None,
                        "coverage_guard_ready": state.coverage_guard.enabled,
                        "coverage_regions": state.coverage_guard.selected_ids,
                        "listen": "127.0.0.1-only",
                    },
                )

            if parsed.path in ("/api/interpreter", "/interpreter"):
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                query = params.get("data", [""])[0]
                return self._handle_query(query)
            return self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path not in ("/api/interpreter", "/interpreter"):
                return self._json(404, {"error": "not_found"})
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 1_000_000:
                return self._json(413, {"error": "query_too_large"})
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "").lower()
            if "application/x-www-form-urlencoded" in ctype:
                params = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
                query = params.get("data", [""])[0]
            else:
                text = body.decode("utf-8")
                # Be forgiving: many Overpass clients send form encoding without Content-Type.
                if text.startswith("data="):
                    query = urllib.parse.parse_qs(text, keep_blank_values=True).get("data", [""])[0]
                else:
                    query = text
            return self._handle_query(query)

        def _handle_query(self, query: str) -> None:
            request_started = time.perf_counter()
            request_id, active_at_start, _max_active_seen, _total_requests = (
                state.begin_query_request()
            )
            status = 500
            results_count = 0
            profile: QueryProfile | None = None
            lock_wait_ms = 0.0
            execute_ms = 0.0
            error_info: dict | None = None
            coverage_diagnostic: dict | None = None

            try:
                if not query:
                    status = 400
                    error_info = {"type": "missing_query"}
                    return self._json(
                        400,
                        {
                            "error": "missing_query",
                            "detail": "Supply OverpassQL as data=<query>.",
                            "_local": {"request_id": request_id},
                        },
                    )

                started = time.perf_counter()
                profile = QueryProfile()
                slot = -1
                con = None
                try:
                    coverage_result = state.coverage_guard.check_query(query)
                    coverage_diagnostic = coverage_result.diagnostic

                    lock_wait_started = time.perf_counter()
                    slot, con = state.acquire()
                    lock_acquired = time.perf_counter()
                    lock_wait_ms = (lock_acquired - lock_wait_started) * 1000.0
                    execute_started = lock_acquired
                    elements = canonicalize_elements(
                        execute_overpass_subset(con, query, profile=profile)
                    )
                    ts = con.execute(
                        "SELECT value FROM metadata WHERE key='osm_timestamp'"
                    ).fetchone()
                    execute_ms = (time.perf_counter() - execute_started) * 1000.0
                except CoverageMiss as exc:
                    status = 503
                    coverage_diagnostic = exc.diagnostic
                    error_info = {
                        "type": "coverage_miss",
                        "detail": str(exc),
                    }
                    return self._json(
                        503,
                        {
                            "version": 0.6,
                            "generator": "H145 Local OSM 0.6",
                            "elements": [],
                            "error": "coverage_miss",
                            "detail": str(exc),
                            "_local": {
                                "request_id": request_id,
                                "coverage": coverage_diagnostic,
                            },
                        },
                    )
                except UnsupportedQuery as exc:
                    status = 400
                    error_info = {
                        "type": "unsupported_query",
                        "detail": str(exc),
                    }
                    return self._json(
                        400,
                        {
                            "error": "unsupported_query",
                            "detail": str(exc),
                            "query": query,
                            "_local": {"request_id": request_id},
                        },
                    )
                finally:
                    if con is not None:
                        state.release(slot, con)

                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                lock_wait_ms = round(lock_wait_ms, 2)
                execute_ms = round(execute_ms, 2)
                # Keep QueryProfile's unprofiled bucket about execution work only.
                # Waiting for an available reader is reported explicitly. Keep the
                # historical lock_wait_ms name as a compatibility alias.
                profile.unprofiled_ms = max(0.0, execute_ms - profile.timed_ms())
                results_count = len(elements)
                status = 200
                local_info = {
                    "request_id": request_id,
                    "query_ms": elapsed_ms,
                    "lock_wait_ms": lock_wait_ms,
                    "reader_wait_ms": lock_wait_ms,
                    "execute_ms": execute_ms,
                    "reader_slot": slot,
                    "db_readers": state.db_readers,
                    "profile": profile.as_dict(),
                }
                if coverage_diagnostic is not None:
                    local_info["coverage"] = coverage_diagnostic
                return self._json(
                    200,
                    {
                        "version": 0.6,
                        "generator": "H145 Local OSM 0.6",
                        "osm3s": {
                            "timestamp_osm_base": ts["value"] if ts else None,
                            "copyright": COPYRIGHT,
                        },
                        "elements": elements,
                        "_local": local_info,
                    },
                )
            except Exception as exc:
                error_info = {
                    "type": type(exc).__name__,
                    "detail": str(exc),
                }
                raise
            finally:
                state.end_query_request()
                duration_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
                event: dict = {
                    "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "id": request_id,
                    "method": self.command,
                    "status": status,
                    "results": results_count,
                    "duration_ms": duration_ms,
                    "query": query.strip(),
                }

                if profile is not None:
                    timings = _useful_timing_ms(profile)
                    if timings:
                        event["timing_ms"] = timings

                if active_at_start > 1 or lock_wait_ms >= 0.10:
                    event["concurrency"] = {
                        "active": active_at_start,
                        "reader_wait_ms": round(lock_wait_ms, 2),
                    }

                if profile is not None:
                    path = _query_path(profile)
                    if path:
                        event["path"] = path

                if coverage_diagnostic is not None:
                    event["coverage"] = coverage_diagnostic

                if error_info is not None:
                    event["error"] = error_info

                _emit_console("API", event)

    return Handler


def serve(db_path: Path, port: int = 8765, db_readers: int = 1) -> None:
    state = State(db_path, db_readers=db_readers)
    # Deliberately hard-coded to loopback. Do not change this to 0.0.0.0.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    print(f"H145 Local OSM listening only on http://127.0.0.1:{port}")
    print(f"Overpass-compatible endpoint: http://127.0.0.1:{port}/api/interpreter")
    print(f"Health: http://127.0.0.1:{port}/health")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        state.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Local-only Overpass compatibility endpoint for R&E")
    p.add_argument("--db", type=Path, default=Path("data/local_osm.sqlite3"))
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--db-readers",
        type=int,
        default=1,
        help="persistent read-only SQLite connections (1-8; default 1)",
    )
    p.add_argument("--seed-demo", action="store_true", help="insert synthetic test objects")
    args = p.parse_args()
    if not (1024 <= args.port <= 65535):
        p.error("port must be between 1024 and 65535")
    if not (1 <= args.db_readers <= 8):
        p.error("--db-readers must be between 1 and 8")

    # Existing imported databases are never schema-mutated by the runtime server.
    # Only the explicit synthetic-demo path creates/seeds a database.
    if args.seed_demo:
        con = connect(args.db)
        seed_demo(con)
        con.close()
    elif not args.db.is_file():
        p.error(f"database not found: {args.db}")

    serve(args.db, args.port, args.db_readers)


if __name__ == "__main__":
    main()
