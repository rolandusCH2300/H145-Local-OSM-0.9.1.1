from __future__ import annotations

"""Build-only SQL grouping. Transaction ownership stays with the importer."""

import sqlite3
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from itertools import chain, groupby, islice


@dataclass(frozen=True, slots=True)
class WriteOptions:
    sql_rows: int = 64
    writer_rows: int = 64_000
    sql_write_mode: str = "grouped"

    def __post_init__(self):
        if not 1 <= self.sql_rows <= 1024:
            raise ValueError("sql_batch_rows must be between 1 and 1024")
        if self.writer_rows < 1:
            raise ValueError("writer_batch_size must be positive")
        if self.sql_write_mode not in {"grouped", "direct"}:
            raise ValueError("sql_write_mode must be 'grouped' or 'direct'")

    @property
    def effective_sql_rows(self) -> int:
        return 1 if self.sql_write_mode == "direct" else self.sql_rows


DEFAULT_WRITE_OPTIONS = WriteOptions()
_OPTIONS: dict[int, WriteOptions] = {}


def register_write_options(con, options: WriteOptions) -> None:
    _OPTIONS[id(con)] = options


def discard_write_options(con) -> None:
    _OPTIONS.pop(id(con), None)


def write_options(con) -> WriteOptions:
    return _OPTIONS.get(id(con), DEFAULT_WRITE_OPTIONS)


def make_write_options(sql_rows=DEFAULT_WRITE_OPTIONS.sql_rows,
                       writer_rows=DEFAULT_WRITE_OPTIONS.writer_rows,
                       sql_write_mode=DEFAULT_WRITE_OPTIONS.sql_write_mode):
    return WriteOptions(int(sql_rows), int(writer_rows), sql_write_mode)


def add_build_write_arguments(parser) -> None:
    parser.add_argument("--sql-write-mode", choices=("grouped", "direct"),
                        default=DEFAULT_WRITE_OPTIONS.sql_write_mode,
                        help="grouped (default); direct restores the previous executemany path for comparison")
    parser.add_argument("--sql-batch-rows", type=int, default=DEFAULT_WRITE_OPTIONS.sql_rows,
                        help="rows per grouped SQL statement; ignored in direct mode")
    parser.add_argument("--writer-batch-size", type=int, default=DEFAULT_WRITE_OPTIONS.writer_rows,
                        help="target ways per writer batch, independent of worker batches")


def build_write_cli_args(args) -> list[str]:
    return ["--sql-write-mode", args.sql_write_mode,
            "--sql-batch-rows", str(args.sql_batch_rows),
            "--writer-batch-size", str(args.writer_batch_size)]


@dataclass(slots=True)
class WriteProfile:
    # Wall intervals overlap across writers; cpu_seconds is thread CPU time.
    prepare_seconds: float = 0.0
    packing_seconds: float = 0.0
    object_sql_seconds: float = 0.0
    geometry_sql_seconds: float = 0.0
    auxiliary_sql_seconds: float = 0.0
    spool_seconds: float = 0.0
    begin_seconds: float = 0.0
    commit_seconds: float = 0.0
    cpu_seconds: float = 0.0
    transactions: int = 0
    object_rows: int = 0
    geometry_rows: int = 0
    geometry_bytes: int = 0
    sql_statements: int = 0
    executemany_calls: int = 0
    max_transaction_rows: int = 0
    max_transaction_bytes: int = 0


    def as_dict(self):
        return asdict(self)


@lru_cache(maxsize=128)
def _insert_sql(prefix: str, width: int, count: int) -> str:
    return prefix + " VALUES " + ",".join(["(" + ",".join(["?"] * width) + ")"] * count)


def grouped_insert(con, prefix: str, width: int, rows, *, sql_rows: int = 64,
                   blob_columns: tuple[int, ...] = (), max_statement_bytes: int = 1024**2,
                   profile: WriteProfile | None = None, kind: str = "object") -> None:
    """Insert bounded chunks with bound parameters, preserving order and blobs.

    SQLite's live variable limit is respected; other drivers use the conservative
    SQLite-compatible 999-parameter limit. A single oversized row is never split.
    Packing is timed separately from SQL. At most 1024 input rows are flattened
    at once, and statements reuse their SQL through executemany and a small cache.
    """
    if not 1 <= sql_rows <= 1024 or width < 1 or max_statement_bytes < 1:
        raise ValueError("invalid grouped insert limits")
    limit = con.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) if isinstance(con, sqlite3.Connection) else 999
    if width > limit:
        raise ValueError(f"row width {width} exceeds database parameter limit {limit}")
    group_rows = min(sql_rows, limit // width)
    iterator = iter(rows)
    while True:
        started = time.perf_counter()
        block = list(islice(iterator, 1024))
        if not block:
            break
        groups = []
        payload_bytes = 0
        if not blob_columns:
            for offset in range(0, len(block), group_rows):
                chunk = block[offset:offset + group_rows]
                groups.append((len(chunk), tuple(chain.from_iterable(chunk))))
        else:
            chunk = []
            chunk_bytes = 0
            for row in block:
                row_bytes = sum(len(row[column]) for column in blob_columns)
                if chunk and (len(chunk) >= group_rows or chunk_bytes + row_bytes > max_statement_bytes):
                    groups.append((len(chunk), tuple(chain.from_iterable(chunk))))
                    chunk = []
                    chunk_bytes = 0
                chunk.append(row)
                chunk_bytes += row_bytes
                payload_bytes += row_bytes
            if chunk:
                groups.append((len(chunk), tuple(chain.from_iterable(chunk))))
        if profile is not None:
            profile.packing_seconds += time.perf_counter() - started
        started = time.perf_counter()
        try:
            for count, same_size in groupby(groups, key=lambda group: group[0]):
                cursor = con.executemany(_insert_sql(prefix, width, count),
                                         (parameters for _, parameters in same_size))
                cursor.close()
                if profile is not None:
                    profile.executemany_calls += 1
        finally:
            if profile is not None:
                field = f"{kind}_sql_seconds"
                setattr(profile, field, getattr(profile, field) + time.perf_counter() - started)
        if profile is not None:
            profile.sql_statements += len(groups)
            # Counts describe successful insert calls; retried transactions can
            # contribute again. ImportStats remains the committed-row authority.
            field = f"{kind}_rows"
            setattr(profile, field, getattr(profile, field) + len(block))
            if kind == "geometry":
                profile.geometry_bytes += payload_bytes


OBJECT_INSERT = ("INSERT INTO objects(pk,osm_type,osm_id,center_lat,center_lon,"
                 "min_lat,max_lat,min_lon,max_lon,area_eligible,has_tags)")
GEOMETRY_INSERT = "INSERT INTO way_geometry_v5(way_id,node_ids,coordinates)"


def direct_insert(con, prefix: str, width: int, rows, *, blob_columns=(),
                  profile: WriteProfile | None = None, kind: str = "object") -> None:
    """One single-row executemany call over the caller's existing batch.

    Import callers already provide bounded lists. Object rows go straight to
    SQLite; geometry retains the previous int/Binary parameter adapters. Neither
    path uses 1,024-row blocks or flattened parameters. Transaction ownership
    is unchanged, with profiling around the original driver operation.
    """
    if not isinstance(rows, (list, tuple)):
        rows = list(rows)
    if not rows:
        return
    # Match db.insert_way_geometry_v5_rows from the previous importer, including
    # its per-row bindings, so this control also covers adapter overhead.
    parameters = (
        ((int(way_id), sqlite3.Binary(node_ids), sqlite3.Binary(coordinates))
         for way_id, node_ids, coordinates in rows)
        if kind == "geometry" else rows
    )
    started = time.perf_counter()
    try:
        cursor = con.executemany(_insert_sql(prefix, width, 1), parameters)
        cursor.close()
    finally:
        if profile is not None:
            field = f"{kind}_sql_seconds"
            setattr(profile, field, getattr(profile, field) + time.perf_counter() - started)
    if profile is not None:
        profile.executemany_calls += 1
        profile.sql_statements += len(rows)
        field = f"{kind}_rows"
        setattr(profile, field, getattr(profile, field) + len(rows))
        if kind == "geometry":
            profile.geometry_bytes += sum(len(row[col]) for row in rows for col in blob_columns)


def _insert_rows(con, prefix, width, rows, *, blob_columns=(), profile=None, kind="object"):
    options = write_options(con)
    if options.sql_write_mode == "direct":
        direct_insert(con, prefix, width, rows, blob_columns=blob_columns,
                      profile=profile, kind=kind)
    else:
        grouped_insert(con, prefix, width, rows, sql_rows=options.sql_rows,
                       blob_columns=blob_columns, profile=profile, kind=kind)


def insert_object_rows(con, rows, *, profile=None):
    _insert_rows(con, OBJECT_INSERT, 11, rows, profile=profile)


def insert_geometry_rows(con, rows, *, profile=None):
    _insert_rows(con, GEOMETRY_INSERT, 3, rows, blob_columns=(1, 2),
                 profile=profile, kind="geometry")
