from __future__ import annotations

import math
import re
import sqlite3
import time
from functools import lru_cache
from dataclasses import dataclass

from .db import (
    get_area_geometry,
    decode_way_geometry,
    decode_way_geometry_node_ids,
    decode_way_geometry_v5,
    decode_way_coordinates,
    decode_way_node_ids,
    get_node_coord,
    get_tags,
    get_tags_many,
    get_way_node_ids,
    get_way_node_ids_by_way,
    get_way_nodes,
    get_way_nodes_by_way,
    get_way_nodes_many,
    get_relation_members_many,
    sqlite_in_chunk_size,
)
from .road_index import (
    ROAD_CLASS_BY_VALUE,
    fixed_node_ids_view,
    road_index_fast_nodes_ready,
    road_index_ready,
)
from .planner_stats import get_persisted_tag_count, get_persisted_values_count
from .tag_spatial_index import feature_for_filter, tag_spatial_index_ready
from .hydration_cache import (
    packed_tags_ready,
    packed_way_ids_ready,
    load_packed_tags_many,
    load_packed_way_ids_many,
)

NUMBER = r"-?\d+(?:\.\d+)?"
FILTER_BLOCK = r"(?:\s*\[[^\]]+\]\s*)*"
BBOX_SELECTOR_RE = re.compile(
    rf"(?P<kind>node|way|area|nwr)\s*"
    rf"(?P<prefix>{FILTER_BLOCK})"
    rf"\(\s*(?P<s>{NUMBER})\s*,\s*(?P<w>{NUMBER})\s*,\s*(?P<n>{NUMBER})\s*,\s*(?P<e>{NUMBER})\s*\)"
    rf"(?P<suffix>{FILTER_BLOCK})",
    re.IGNORECASE,
)

AROUND_SELECTOR_RE = re.compile(
    rf"(?P<kind>node|way|area|nwr)\s*"
    rf"(?P<prefix>{FILTER_BLOCK})"
    rf"\(\s*around\s*:\s*(?P<radius>{NUMBER})\s*,\s*(?P<lat>{NUMBER})\s*,\s*(?P<lon>{NUMBER})\s*\)"
    rf"(?P<suffix>{FILTER_BLOCK})",
    re.IGNORECASE,
)
FILTER_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z0-9_:.-]+)\s*"
    r"(?:(?P<op>!=|=|~)\s*"
    r"(?:(?:\"(?P<dq>[^\"]*)\")|(?:'(?P<sq>[^']*)')|(?P<bare>.*?)))?\s*$",
    re.DOTALL,
)
FILTER_BLOCK_RE = re.compile(r"\[([^\]]+)\]")

ROAD_INTERSECTION_MARKER = "node(w.major)(w.minor)"
RECURSE_DOWN_MARKER_RE = re.compile(r"\(\s*\._\s*;\s*>\s*;\s*\)\s*;", re.IGNORECASE)

IS_IN_PIVOT_RE = re.compile(
    rf"is_in\(\s*(?P<lat>{NUMBER})\s*,\s*(?P<lon>{NUMBER})\s*\)\s*"
    rf"->\s*\.(?P<set>[A-Za-z0-9_]+)\s*;\s*"
    rf"rel\s*\(\s*pivot\.(?P<pivot_set>[A-Za-z0-9_]+)\s*\)"
    rf"(?P<filters>{FILTER_BLOCK})\s*;\s*out\s+tags\s*;?",
    re.IGNORECASE,
)


class UnsupportedQuery(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TagFilter:
    key: str
    op: str  # exists, =, !=, ~
    value: str | None = None


@dataclass(frozen=True, slots=True)
class Selector:
    kind: str
    south: float
    west: float
    north: float
    east: float
    filters: tuple[TagFilter, ...]
    spatial_mode: str = "bbox"
    center_lat: float | None = None
    center_lon: float | None = None
    radius_m: float | None = None


@dataclass(slots=True)
class QueryProfile:
    # Top-level timing buckets are mutually exclusive.
    parse_ms: float = 0.0
    candidate_sql_ms: float = 0.0
    candidate_filter_ms: float = 0.0
    hydrate_ms: float = 0.0
    output_ms: float = 0.0
    recurse_ms: float = 0.0
    coverage_prepare_ms: float = 0.0
    coverage_filter_ms: float = 0.0
    # Diagnostic sub-stages are excluded from timed_ms().
    hydrate_tags_ms: float = 0.0
    hydrate_tags_sql_ms: float = 0.0
    hydrate_tags_decode_ms: float = 0.0
    hydrate_way_ms: float = 0.0
    hydrate_way_sql_ms: float = 0.0
    hydrate_way_decode_ms: float = 0.0
    hydrate_relations_ms: float = 0.0
    recurse_relations_ms: float = 0.0
    recurse_coordmap_ms: float = 0.0
    recurse_node_filter_ms: float = 0.0
    recurse_node_sql_ms: float = 0.0
    recurse_emit_ms: float = 0.0
    road_topology_ms: float = 0.0
    road_hydrate_ms: float = 0.0
    road_output_ms: float = 0.0
    is_in_sql_ms: float = 0.0
    is_in_hydrate_ms: float = 0.0
    is_in_geometry_ms: float = 0.0
    is_in_output_ms: float = 0.0
    unprofiled_ms: float = 0.0
    candidate_calls: int = 0
    tag_seeded_calls: int = 0
    exists_tag_seeded_calls: int = 0
    spatial_seeded_calls: int = 0
    adaptive_tag_first_calls: int = 0
    adaptive_spatial_first_calls: int = 0
    road_index_calls: int = 0
    tag_spatial_index_calls: int = 0
    packed_tags_calls: int = 0
    packed_way_ids_calls: int = 0
    candidate_rows: int = 0
    matched_rows: int = 0

    def timed_ms(self) -> float:
        return (
            self.parse_ms + self.candidate_sql_ms + self.candidate_filter_ms
            + self.hydrate_ms + self.output_ms + self.recurse_ms
            + self.road_topology_ms + self.road_hydrate_ms + self.road_output_ms
            + self.is_in_sql_ms + self.is_in_hydrate_ms + self.is_in_geometry_ms + self.is_in_output_ms
            + self.coverage_prepare_ms + self.coverage_filter_ms
        )

    def as_dict(self, *, include_coverage: bool = False) -> dict[str, float | int]:
        # Keep the existing HTTP profile shape. Benchmarks can opt into explicit
        # coverage stages; legacy telemetry accounts for these as unprofiled.
        coverage_ms = self.coverage_prepare_ms + self.coverage_filter_ms
        result = {
            "parse_ms": round(self.parse_ms, 2),
            "candidate_sql_ms": round(self.candidate_sql_ms, 2),
            "candidate_filter_ms": round(self.candidate_filter_ms, 2),
            "hydrate_ms": round(self.hydrate_ms, 2),
            "output_ms": round(self.output_ms, 2),
            "recurse_ms": round(self.recurse_ms, 2),
            "hydrate_tags_ms": round(self.hydrate_tags_ms, 2),
            "hydrate_tags_sql_ms": round(self.hydrate_tags_sql_ms, 2),
            "hydrate_tags_decode_ms": round(self.hydrate_tags_decode_ms, 2),
            "hydrate_way_ms": round(self.hydrate_way_ms, 2),
            "hydrate_way_sql_ms": round(self.hydrate_way_sql_ms, 2),
            "hydrate_way_decode_ms": round(self.hydrate_way_decode_ms, 2),
            "hydrate_relations_ms": round(self.hydrate_relations_ms, 2),
            "recurse_relations_ms": round(self.recurse_relations_ms, 2),
            "recurse_coordmap_ms": round(self.recurse_coordmap_ms, 2),
            "recurse_node_filter_ms": round(self.recurse_node_filter_ms, 2),
            "recurse_node_sql_ms": round(self.recurse_node_sql_ms, 2),
            "recurse_emit_ms": round(self.recurse_emit_ms, 2),
            "road_topology_ms": round(self.road_topology_ms, 2),
            "road_hydrate_ms": round(self.road_hydrate_ms, 2),
            "road_output_ms": round(self.road_output_ms, 2),
            "is_in_sql_ms": round(self.is_in_sql_ms, 2),
            "is_in_hydrate_ms": round(self.is_in_hydrate_ms, 2),
            "is_in_geometry_ms": round(self.is_in_geometry_ms, 2),
            "is_in_output_ms": round(self.is_in_output_ms, 2),
            "timed_ms": round(self.timed_ms() - (0.0 if include_coverage else coverage_ms), 2),
            "unprofiled_ms": round(self.unprofiled_ms + (0.0 if include_coverage else coverage_ms), 2),
            "candidate_calls": self.candidate_calls,
            "tag_seeded_calls": self.tag_seeded_calls,
            "exists_tag_seeded_calls": self.exists_tag_seeded_calls,
            "spatial_seeded_calls": self.spatial_seeded_calls,
            "adaptive_tag_first_calls": self.adaptive_tag_first_calls,
            "adaptive_spatial_first_calls": self.adaptive_spatial_first_calls,
            "road_index_calls": self.road_index_calls,
            "tag_spatial_index_calls": self.tag_spatial_index_calls,
            "packed_tags_calls": self.packed_tags_calls,
            "packed_way_ids_calls": self.packed_way_ids_calls,
            "candidate_rows": self.candidate_rows,
            "matched_rows": self.matched_rows,
        }
        if include_coverage:
            result["coverage_prepare_ms"] = round(self.coverage_prepare_ms, 2)
            result["coverage_filter_ms"] = round(self.coverage_filter_ms, 2)
        return result


def _parse_filter_blocks(text: str) -> tuple[TagFilter, ...]:
    out: list[TagFilter] = []
    for block in FILTER_BLOCK_RE.findall(text or ""):
        m = FILTER_RE.match(block)
        if not m:
            raise UnsupportedQuery(f"unsupported tag filter: [{block}]")
        key = m.group("key")
        op = m.group("op") or "exists"
        if op == "exists":
            value = None
        else:
            value = next((x for x in (m.group("dq"), m.group("sq"), m.group("bare")) if x is not None), "")
            value = value.strip()
        out.append(TagFilter(key=key, op=op, value=value))
    return tuple(out)


def _around_bounds(radius_m: float, lat: float, lon: float) -> tuple[float, float, float, float]:
    if radius_m < 0:
        raise UnsupportedQuery("around radius must be non-negative")
    # Conservative WGS84-ish envelope for the RTree prefilter. Exact matching is
    # applied later using a great-circle distance against the stored object center.
    lat_delta = radius_m / 111_320.0
    cos_lat = max(abs(math.cos(math.radians(lat))), 1e-6)
    lon_delta = radius_m / (111_320.0 * cos_lat)
    return (
        max(-90.0, lat - lat_delta),
        max(-180.0, lon - lon_delta),
        min(90.0, lat + lat_delta),
        min(180.0, lon + lon_delta),
    )


def parse_selectors(query: str) -> list[Selector]:
    matches: list[tuple[int, Selector]] = []

    for m in BBOX_SELECTOR_RE.finditer(query):
        filters = _parse_filter_blocks((m.group("prefix") or "") + (m.group("suffix") or ""))
        matches.append((m.start(), Selector(
            kind=m.group("kind").lower(),
            south=float(m.group("s")),
            west=float(m.group("w")),
            north=float(m.group("n")),
            east=float(m.group("e")),
            filters=filters,
        )))

    for m in AROUND_SELECTOR_RE.finditer(query):
        filters = _parse_filter_blocks((m.group("prefix") or "") + (m.group("suffix") or ""))
        radius_m = float(m.group("radius"))
        lat = float(m.group("lat"))
        lon = float(m.group("lon"))
        south, west, north, east = _around_bounds(radius_m, lat, lon)
        matches.append((m.start(), Selector(
            kind=m.group("kind").lower(),
            south=south,
            west=west,
            north=north,
            east=east,
            filters=filters,
            spatial_mode="around",
            center_lat=lat,
            center_lon=lon,
            radius_m=radius_m,
        )))

    matches.sort(key=lambda item: item[0])
    return [selector for _, selector in matches]


def _filter_matches(tags: dict[str, str], flt: TagFilter) -> bool:
    if flt.op == "exists":
        return flt.key in tags
    if flt.op == "=":
        return tags.get(flt.key) == flt.value
    if flt.op == "!=":
        # Overpass's [key!=value] also matches objects where key is absent.
        return tags.get(flt.key) != flt.value
    if flt.op == "~":
        value = tags.get(flt.key)
        if value is None:
            return False
        try:
            return re.search(flt.value or "", value) is not None
        except re.error as exc:
            raise UnsupportedQuery(f"invalid regex in query: {exc}") from exc
    raise UnsupportedQuery(f"unsupported filter operator: {flt.op}")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


ADAPTIVE_SMALL_TAG_LIMIT = 20_000
ADAPTIVE_COUNT_CAP = 100_000


@lru_cache(maxsize=16)
def _tag_spatial_bundle_ready(con: sqlite3.Connection) -> bool:
    """Cache immutable-database tag-spatial readiness per query connection."""
    return tag_spatial_index_ready(con)


def _tag_spatial_seed_filter(sel: Selector) -> tuple[TagFilter, str] | None:
    """Return a proven-expensive exact filter and its dedicated RTree table.

    The adaptive planner still decides whether this selector should be spatial
    first. This helper is only consulted after that decision, so huge-radius
    queries that are cheaper tag-first are never forced through an RTree.
    """
    for flt in sel.filters:
        if flt.op != "=" or flt.value is None:
            continue
        feature = feature_for_filter(flt.key, flt.value)
        if feature is not None:
            return flt, feature.table
    return None


def _type_sql_for_kind(kind: str) -> str:
    if kind == "node":
        return "o.osm_type='node'"
    if kind == "way":
        return "o.osm_type='way'"
    if kind == "area":
        return "o.area_eligible=1"
    if kind == "nwr":
        return "1=1"
    raise UnsupportedQuery(f"unsupported selector type: {kind}")


@lru_cache(maxsize=512)
def _bounded_exact_tag_count(
    con: sqlite3.Connection, key: str, value: str | None, cap: int
) -> int:
    """Return tag cardinality, capped at cap+1.

    New databases persist exact R&E-profile counts at import time. Older DBs
    transparently fall back to bounded covering-index scans.
    """
    persisted = get_persisted_tag_count(con, key, value)
    if persisted is not None:
        return min(int(persisted), cap + 1)
    if value is None:
        sql = (
            "SELECT COUNT(*) FROM (SELECT 1 FROM object_tags INDEXED BY idx_tags_key "
            "WHERE key=? LIMIT ?)"
        )
        params = (key, cap + 1)
    else:
        sql = (
            "SELECT COUNT(*) FROM (SELECT 1 FROM object_tags INDEXED BY idx_tags_key_value "
            "WHERE key=? AND value=? LIMIT ?)"
        )
        params = (key, value, cap + 1)
    return int(con.execute(sql, params).fetchone()[0])


@lru_cache(maxsize=512)
def _bounded_values_tag_count(
    con: sqlite3.Connection, key: str, values: tuple[str, ...], cap: int
) -> int:
    if not values:
        return 0
    persisted = get_persisted_values_count(con, key, values)
    if persisted is not None:
        return min(int(persisted), cap + 1)
    placeholders = ",".join("?" for _ in values)
    sql = (
        "SELECT COUNT(*) FROM (SELECT 1 FROM object_tags INDEXED BY idx_tags_key_value "
        f"WHERE key=? AND value IN ({placeholders}) LIMIT ?)"
    )
    return int(con.execute(sql, (key, *values, cap + 1)).fetchone()[0])


@lru_cache(maxsize=1024)
def _bounded_spatial_count(
    con: sqlite3.Connection,
    kind: str,
    south: float,
    west: float,
    north: float,
    east: float,
    cap: int,
) -> int:
    # nwr needs no objects join to estimate the RTree start cardinality.
    if kind == "nwr":
        sql = (
            "SELECT COUNT(*) FROM (SELECT 1 FROM object_rtree r "
            "WHERE r.min_lon<=? AND r.max_lon>=? AND r.min_lat<=? AND r.max_lat>=? LIMIT ?)"
        )
        params = (east, west, north, south, cap + 1)
    else:
        type_sql = _type_sql_for_kind(kind)
        sql = (
            "SELECT COUNT(*) FROM (SELECT 1 FROM object_rtree r CROSS JOIN objects o ON o.pk=r.pk "
            "WHERE r.min_lon<=? AND r.max_lon>=? AND r.min_lat<=? AND r.max_lat>=? "
            f"AND {type_sql} LIMIT ?)"
        )
        params = (east, west, north, south, cap + 1)
    return int(con.execute(sql, params).fetchone()[0])


def _tag_seed_count(
    con: sqlite3.Connection,
    flt: TagFilter,
    regex_values: tuple[str, ...] | None,
    cap: int,
) -> int | None:
    if flt.op == "=":
        return _bounded_exact_tag_count(con, flt.key, flt.value or "", cap)
    if flt.op == "exists":
        return _bounded_exact_tag_count(con, flt.key, None, cap)
    if flt.op == "~" and regex_values is not None:
        return _bounded_values_tag_count(con, flt.key, regex_values, cap)
    return None


def _candidate_seed(
    con: sqlite3.Connection,
    sel: Selector,
    regex_values: dict[int, tuple[str, ...] | None],
    profile: QueryProfile | None = None,
) -> TagFilter | None:
    """Choose tag-first or spatial-first from bounded cardinality estimates.

    Selective tags skip the spatial probe entirely so 7 ms hospital-style
    queries stay cheap. Common tags are compared with the local RTree envelope;
    this lets building-heavy queries prefer spatial-first while large-radius
    sparse queries still prefer the tag index.
    """
    candidates: list[tuple[int, int, TagFilter]] = []
    for i, flt in enumerate(sel.filters):
        values = regex_values.get(i) if flt.op == "~" else None
        count = _tag_seed_count(con, flt, values, ADAPTIVE_COUNT_CAP)
        if count is not None:
            candidates.append((count, i, flt))
    if not candidates:
        if profile is not None:
            profile.adaptive_spatial_first_calls += 1
        return None

    tag_count, _idx, best = min(candidates, key=lambda item: item[0])
    # A small national tag set is necessarily at least as selective as any
    # useful local spatial scan; avoid paying for an extra RTree count.
    if tag_count <= ADAPTIVE_SMALL_TAG_LIMIT:
        if profile is not None:
            profile.adaptive_tag_first_calls += 1
        return best

    spatial_count = _bounded_spatial_count(
        con, sel.kind, sel.south, sel.west, sel.north, sel.east, ADAPTIVE_COUNT_CAP
    )
    if spatial_count < tag_count:
        if profile is not None:
            profile.adaptive_spatial_first_calls += 1
        return None
    if tag_count < spatial_count:
        if profile is not None:
            profile.adaptive_tag_first_calls += 1
        return best

    # Both sides hit the cap. Local envelopes are normally cheaper spatially;
    # very large HPG search envelopes are cheaper from the tag index.
    if sel.spatial_mode == "around" and sel.radius_m is not None:
        choose_tag = sel.radius_m >= 100_000.0
    else:
        approx_span = max(0.0, sel.north - sel.south) * max(0.0, sel.east - sel.west)
        choose_tag = approx_span >= 4.0
    if profile is not None:
        if choose_tag:
            profile.adaptive_tag_first_calls += 1
        else:
            profile.adaptive_spatial_first_calls += 1
    return best if choose_tag else None


@lru_cache(maxsize=128)
def _tag_values_for_key(con: sqlite3.Connection, key: str) -> tuple[str, ...]:
    """Enumerate the exact vocabulary without scanning every duplicate tag.

    The covering index orders values within each key. Seeking past the previous
    value keeps category regex setup proportional to vocabulary size, even on
    continent-sized DBs. A high-cardinality key switches to a remaining-value
    scan to avoid an unbounded number of individual seeks; no values are capped.
    """
    row = con.execute(
        "SELECT value FROM object_tags INDEXED BY idx_tags_key_value "
        "WHERE key=? ORDER BY value LIMIT 1", (key,),
    ).fetchone()
    values: list[str] = []
    while row is not None:
        value = str(row[0])
        values.append(value)
        if len(values) >= 4096:
            values.extend(
                str(item[0]) for item in con.execute(
                    "SELECT DISTINCT value FROM object_tags INDEXED BY idx_tags_key_value "
                    "WHERE key=? AND value>? ORDER BY value", (key, value),
                )
            )
            break
        row = con.execute(
            "SELECT value FROM object_tags INDEXED BY idx_tags_key_value "
            "WHERE key=? AND value>? ORDER BY value LIMIT 1", (key, value),
        ).fetchone()
    return tuple(values)


@lru_cache(maxsize=256)
def _regex_matching_values(
    con: sqlite3.Connection, key: str, pattern: str
) -> tuple[str, ...] | None:
    """Resolve a regex against the finite value domain for one OSM tag key.

    R&E's regexes are category selectors (building/highway), where the key has
    a small value vocabulary. Turning the regex into exact values lets SQLite
    use ``idx_tags_key_value`` instead of scanning every object that owns the key.
    Multiple regexes for the same key share one cached exact vocabulary.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise UnsupportedQuery(f"invalid regex in query: {exc}") from exc
    values = _tag_values_for_key(con, key)
    matched = tuple(value for value in values if compiled.search(value) is not None)
    # Keep well below SQLite's common 999-parameter limit. R&E's current regex
    # vocabulary resolves to only a handful of values.
    if len(matched) > 400:
        return None
    return matched


def _candidate_rows(
    con: sqlite3.Connection,
    sel: Selector,
    profile: QueryProfile | None = None,
) -> list[sqlite3.Row]:
    type_sql: list[str] = []
    if sel.kind == "node":
        type_sql.append("o.osm_type='node'")
    elif sel.kind == "way":
        type_sql.append("o.osm_type='way'")
    elif sel.kind == "area":
        type_sql.append("o.area_eligible=1")
    elif sel.kind != "nwr":
        raise UnsupportedQuery(f"unsupported selector type: {sel.kind}")

    # Resolve category-style regexes (all current R&E regexes are of this form)
    # to exact tag values. This converts e.g. highway~"^(primary|secondary)$"
    # into an indexed key/value lookup rather than a scan of every highway tag.
    regex_values: dict[int, tuple[str, ...] | None] = {}
    for i, flt in enumerate(sel.filters):
        if flt.op != "~":
            continue
        resolve_started = time.perf_counter()
        values = _regex_matching_values(con, flt.key, flt.value or "")
        if profile is not None:
            profile.candidate_sql_ms += (time.perf_counter() - resolve_started) * 1000.0
        regex_values[i] = values
        if values == ():
            if profile is not None:
                profile.candidate_calls += 1
            return []

    seed_started = time.perf_counter()
    seed = _candidate_seed(con, sel, regex_values, profile)
    if profile is not None:
        profile.candidate_sql_ms += (time.perf_counter() - seed_started) * 1000.0
    seed_index = next((i for i, flt in enumerate(sel.filters) if flt is seed), None)
    if seed is not None and seed.op == "~" and regex_values.get(seed_index) is None:
        # Broad regex: prefer spatial-first + indexed key existence rather than
        # enumerating the entire key domain as the driving table.
        seed = None
        seed_index = None

    # Use a selective RTree for registered exact predicates after spatial-first selection.
    tag_spatial_seed: tuple[TagFilter, str] | None = None
    if seed is None and _tag_spatial_bundle_ready(con):
        tag_spatial_seed = _tag_spatial_seed_filter(sel)

    residual_regex: list[tuple[int, TagFilter]] = [
        (i, flt)
        for i, flt in enumerate(sel.filters)
        if flt.op == "~" and regex_values.get(i) is None
    ]

    # Broad regexes are evaluated in Python, but their values are fetched as
    # scalar indexed lookups inside the candidate SQL. This avoids an N+1 query
    # per candidate while preserving Python regex semantics.
    select_parts = ["o.*"]
    select_params: list[object] = []
    for alias_i, (_filter_i, flt) in enumerate(residual_regex):
        select_parts.append(
            f"(SELECT rx{alias_i}.value FROM object_tags rx{alias_i} "
            f"WHERE rx{alias_i}.object_pk=o.pk AND rx{alias_i}.key=?) AS __rx{alias_i}"
        )
        select_params.append(flt.key)

    where: list[str] = []
    spatial_params: list[object] = [sel.east, sel.west, sel.north, sel.south]

    seed_params: list[object] = []
    if seed is None and tag_spatial_seed is not None:
        feature_filter, feature_table = tag_spatial_seed
        # One exact R&E predicate per RTree means there is no tag join or
        # correlated EXISTS() at all on this path. The RTree returns only objects
        # already known to own that key/value pair.
        from_sql = f"{feature_table} r CROSS JOIN objects o ON o.pk=r.object_pk"
        where.extend([
            "r.min_lon <= ?", "r.max_lon >= ?",
            "r.min_lat <= ?", "r.max_lat >= ?",
        ])
        if profile is not None:
            profile.spatial_seeded_calls += 1
            profile.tag_spatial_index_calls += 1
    elif seed is None:
        # Generic spatial-first fallback needs the full object RTree to discover
        # the local object set and then checks tag predicates against object_tags.
        from_sql = "object_rtree r CROSS JOIN objects o ON o.pk=r.pk"
        where.extend([
            "r.min_lon <= ?", "r.max_lon >= ?",
            "r.min_lat <= ?", "r.max_lat >= ?",
        ])
        if profile is not None:
            profile.spatial_seeded_calls += 1
    elif seed.op == "=":
        # Tag-first already has the object PK from object_tags. The objects table
        # stores the same bbox as object_rtree, so a second RTree lookup per tag
        # candidate is redundant and expensive on national tag sets.
        from_sql = (
            "object_tags seed INDEXED BY idx_tags_key_value "
            "CROSS JOIN objects o ON o.pk=seed.object_pk"
        )
        where.extend([
            "o.min_lon <= ?", "o.max_lon >= ?",
            "o.min_lat <= ?", "o.max_lat >= ?",
        ])
        where.insert(0, "seed.key=? AND seed.value=?")
        seed_params.extend([seed.key, seed.value or ""])
        if profile is not None:
            profile.tag_seeded_calls += 1
    elif seed.op == "~":
        values = regex_values[int(seed_index)]
        assert values is not None and len(values) > 0
        from_sql = (
            "object_tags seed INDEXED BY idx_tags_key_value "
            "CROSS JOIN objects o ON o.pk=seed.object_pk"
        )
        where.extend([
            "o.min_lon <= ?", "o.max_lon >= ?",
            "o.min_lat <= ?", "o.max_lat >= ?",
        ])
        placeholders = ",".join("?" for _ in values)
        where.insert(0, f"seed.key=? AND seed.value IN ({placeholders})")
        seed_params.append(seed.key)
        seed_params.extend(values)
        if profile is not None:
            profile.tag_seeded_calls += 1
    elif seed.op == "exists":
        from_sql = (
            "object_tags seed INDEXED BY idx_tags_key "
            "CROSS JOIN objects o ON o.pk=seed.object_pk"
        )
        where.extend([
            "o.min_lon <= ?", "o.max_lon >= ?",
            "o.min_lat <= ?", "o.max_lat >= ?",
        ])
        where.insert(0, "seed.key=?")
        seed_params.append(seed.key)
        if profile is not None:
            profile.tag_seeded_calls += 1
            profile.exists_tag_seeded_calls += 1
    else:
        raise AssertionError("unexpected candidate seed")

    where.extend(type_sql)

    # Push all exact predicates into SQL. [key!=value] is represented as
    # NOT EXISTS(key=value), preserving the established behavior where objects
    # without the key also match the negative filter.
    filter_params: list[object] = []
    for i, flt in enumerate(sel.filters):
        if flt is seed and flt.op in ("=", "~", "exists"):
            continue
        if tag_spatial_seed is not None and flt is tag_spatial_seed[0] and flt.op == "=":
            # Dedicated feature RTree already guarantees this exact predicate.
            continue
        if flt.op == "=":
            where.append(
                f"EXISTS (SELECT 1 FROM object_tags f{i} "
                f"WHERE f{i}.object_pk=o.pk AND f{i}.key=? AND f{i}.value=?)"
            )
            filter_params.extend([flt.key, flt.value or ""])
        elif flt.op == "exists":
            where.append(
                f"EXISTS (SELECT 1 FROM object_tags f{i} "
                f"WHERE f{i}.object_pk=o.pk AND f{i}.key=?)"
            )
            filter_params.append(flt.key)
        elif flt.op == "!=":
            where.append(
                f"NOT EXISTS (SELECT 1 FROM object_tags f{i} "
                f"WHERE f{i}.object_pk=o.pk AND f{i}.key=? AND f{i}.value=?)"
            )
            filter_params.extend([flt.key, flt.value or ""])
        elif flt.op == "~":
            values = regex_values.get(i)
            if values is not None:
                placeholders = ",".join("?" for _ in values)
                where.append(
                    f"EXISTS (SELECT 1 FROM object_tags f{i} "
                    f"WHERE f{i}.object_pk=o.pk AND f{i}.key=? "
                    f"AND f{i}.value IN ({placeholders}))"
                )
                filter_params.append(flt.key)
                filter_params.extend(values)
            else:
                where.append(
                    f"EXISTS (SELECT 1 FROM object_tags f{i} "
                    f"WHERE f{i}.object_pk=o.pk AND f{i}.key=?)"
                )
                filter_params.append(flt.key)
        else:
            raise UnsupportedQuery(f"unsupported filter operator: {flt.op}")

    sql = f"SELECT {', '.join(select_parts)} FROM {from_sql} WHERE {' AND '.join(where)}"
    # SQLite binds placeholders in textual order: SELECT expressions first,
    # then FROM/WHERE. Seed predicates are inserted before spatial predicates.
    params = select_params + seed_params + spatial_params + filter_params

    started = time.perf_counter()
    rows = list(con.execute(sql, params))
    if profile is not None:
        profile.candidate_sql_ms += (time.perf_counter() - started) * 1000.0
        profile.candidate_calls += 1
        profile.candidate_rows += len(rows)

    filter_started = time.perf_counter()
    out: list[sqlite3.Row] = []
    for row in rows:
        if sel.spatial_mode == "around":
            assert sel.center_lat is not None and sel.center_lon is not None and sel.radius_m is not None
            # Nodes have point geometry and can be checked exactly. For ways and
            # relations the RTree has already established bbox overlap with the
            # conservative circle envelope. Keeping them avoids incorrectly rejecting
            # a long way/area whose centre lies outside the radius but geometry crosses it.
            if row["osm_type"] == "node" and _haversine_m(
                sel.center_lat, sel.center_lon, float(row["center_lat"]), float(row["center_lon"])
            ) > sel.radius_m:
                continue

        regex_ok = True
        for alias_i, (_filter_i, flt) in enumerate(residual_regex):
            value = row[f"__rx{alias_i}"]
            if value is None:
                regex_ok = False
                break
            try:
                if re.search(flt.value or "", str(value)) is None:
                    regex_ok = False
                    break
            except re.error as exc:
                raise UnsupportedQuery(f"invalid regex in query: {exc}") from exc
        if regex_ok:
            out.append(row)

    if profile is not None:
        profile.candidate_filter_ms += (time.perf_counter() - filter_started) * 1000.0
        profile.matched_rows += len(out)
    return out

def _area_id(osm_type: str, osm_id: int) -> int:
    if osm_type == "way":
        return 2_400_000_000 + osm_id
    if osm_type == "relation":
        return 3_600_000_000 + osm_id
    return osm_id


def _way_node_ids(con: sqlite3.Connection, way_id: int) -> list[int]:
    return get_way_node_ids(con, way_id)


def _relation_members(con: sqlite3.Connection, relation_id: int) -> list[dict]:
    return [
        {"type": r["member_type"], "ref": int(r["member_id"]), "role": r["role"]}
        for r in con.execute(
            "SELECT member_type,member_id,role FROM relation_members WHERE relation_id=? ORDER BY seq",
            (relation_id,),
        )
    ]



@lru_cache(maxsize=8)
def _hydration_cache_flags(con: sqlite3.Connection) -> tuple[bool, bool]:
    """Cache immutable per-server derived-hydration availability."""
    return packed_tags_ready(con), packed_way_ids_ready(con)


def _load_tags_profiled(
    con: sqlite3.Connection,
    object_pks: list[int],
    *,
    use_packed: bool,
) -> tuple[dict[int, dict[str, str]], float, float]:
    if use_packed:
        return load_packed_tags_many(con, object_pks)

    pks = list(dict.fromkeys(int(v) for v in object_pks))
    out: dict[int, dict[str, str]] = {pk: {} for pk in pks}
    sql_ms = 0.0
    decode_ms = 0.0
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(pks), chunk_size):
        chunk = pks[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        started = time.perf_counter()
        rows = con.execute(
            f"SELECT object_pk,key,value FROM object_tags "
            f"WHERE object_pk IN ({placeholders}) ORDER BY object_pk",
            chunk,
        ).fetchall()
        sql_ms += (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        for row in rows:
            out[int(row[0])][str(row[1])] = str(row[2])
        decode_ms += (time.perf_counter() - started) * 1000.0
    return out, sql_ms, decode_ms


def _load_way_ids_profiled(
    con: sqlite3.Connection,
    way_ids: list[int],
    *,
    use_packed: bool,
) -> tuple[dict[int, list[int]], float, float]:
    if use_packed:
        return load_packed_way_ids_many(con, way_ids)

    ids = list(dict.fromkeys(int(v) for v in way_ids))
    out: dict[int, list[int]] = {wid: [] for wid in ids}
    sql_ms = 0.0
    decode_ms = 0.0
    if not ids:
        return out, sql_ms, decode_ms
    chunk_size = sqlite_in_chunk_size(con)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        found: set[int] = set()

        # Schema 5 is the native ID-only fast path: SQLite reads only the
        # node_ids column and never touches the aligned coordinate payload.
        started = time.perf_counter()
        rows_v5 = con.execute(
            f"SELECT way_id,node_ids FROM way_geometry_v5 WHERE way_id IN ({placeholders})",
            chunk,
        ).fetchall()
        sql_ms += (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        for row in rows_v5:
            wid = int(row[0])
            found.add(wid)
            out[wid] = decode_way_node_ids(row[1])
        decode_ms += (time.perf_counter() - started) * 1000.0

        # Backward-compatible schema-4 path.
        remaining_v4 = [wid for wid in chunk if wid not in found]
        if remaining_v4:
            rp = ",".join("?" for _ in remaining_v4)
            started = time.perf_counter()
            rows_v4 = con.execute(
                f"SELECT way_id,node_data FROM way_geometry WHERE way_id IN ({rp})",
                remaining_v4,
            ).fetchall()
            sql_ms += (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            for row in rows_v4:
                wid = int(row[0])
                found.add(wid)
                out[wid] = decode_way_geometry_node_ids(row[1])
            decode_ms += (time.perf_counter() - started) * 1000.0

        remaining = [wid for wid in chunk if wid not in found]
        if remaining:
            started = time.perf_counter()
            fallback = get_way_node_ids_by_way(con, remaining)
            sql_ms += (time.perf_counter() - started) * 1000.0
            out.update(fallback)
    return out, sql_ms, decode_ms


def _load_way_geometry_profiled(
    con: sqlite3.Connection,
    way_ids: list[int],
) -> tuple[dict[int, list[tuple[int, float | None, float | None]]], float, float]:
    ids = list(dict.fromkeys(int(v) for v in way_ids))
    out: dict[int, list[tuple[int, float | None, float | None]]] = {wid: [] for wid in ids}
    sql_ms = 0.0
    decode_ms = 0.0
    if not ids:
        return out, sql_ms, decode_ms
    chunk_size = sqlite_in_chunk_size(con)
    # Sort multi-batch schema-v5 lookups by way ID to improve key locality.
    fetch_ids = sorted(ids) if len(ids) > chunk_size else ids
    for offset in range(0, len(fetch_ids), chunk_size):
        chunk = fetch_ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        found: set[int] = set()

        started = time.perf_counter()
        rows_v5 = con.execute(
            f"SELECT way_id,node_ids,coordinates FROM way_geometry_v5 "
            f"WHERE way_id IN ({placeholders})",
            chunk,
        ).fetchall()
        sql_ms += (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        for row in rows_v5:
            wid = int(row[0])
            found.add(wid)
            out[wid] = decode_way_geometry_v5(row[1], row[2])
        decode_ms += (time.perf_counter() - started) * 1000.0

        remaining_v4 = [wid for wid in chunk if wid not in found]
        if remaining_v4:
            rp = ",".join("?" for _ in remaining_v4)
            started = time.perf_counter()
            rows_v4 = con.execute(
                f"SELECT way_id,node_data FROM way_geometry WHERE way_id IN ({rp})",
                remaining_v4,
            ).fetchall()
            sql_ms += (time.perf_counter() - started) * 1000.0
            started = time.perf_counter()
            for row in rows_v4:
                wid = int(row[0])
                found.add(wid)
                out[wid] = decode_way_geometry(row[1])
            decode_ms += (time.perf_counter() - started) * 1000.0

        remaining = [wid for wid in chunk if wid not in found]
        if remaining:
            started = time.perf_counter()
            fallback = get_way_nodes_by_way(con, remaining)
            sql_ms += (time.perf_counter() - started) * 1000.0
            out.update(fallback)
    return out, sql_ms, decode_ms


class _ElementLoader:
    """Batch and cache result hydration for one Overpass request."""

    def __init__(self, con: sqlite3.Connection, profile: QueryProfile | None = None):
        self.con = con
        self.profile = profile
        self.tags: dict[int, dict[str, str]] = {}
        self.way_nodes: dict[int, list[int]] = {}
        self.way_geometry: dict[int, list[tuple[int, float | None, float | None]]] = {}
        self.relation_members: dict[int, list[dict]] = {}
        self.use_packed_tags, self.use_packed_way_ids = _hydration_cache_flags(con)

    def preload(
        self,
        rows: list[sqlite3.Row],
        *,
        way_mode: str = "ids",  # none | ids | geometry
        include_relations: bool = True,
        phase: str = "hydrate",
    ) -> None:
        if not rows:
            return
        pks = [int(r["pk"]) for r in rows if int(r["pk"]) not in self.tags]
        if pks:
            started = time.perf_counter()
            loaded_tags, sql_ms, decode_ms = _load_tags_profiled(
                self.con, pks, use_packed=self.use_packed_tags
            )
            self.tags.update(loaded_tags)
            elapsed = (time.perf_counter() - started) * 1000.0
            if self.profile is not None:
                if self.use_packed_tags:
                    self.profile.packed_tags_calls += 1
                if phase == "hydrate":
                    self.profile.hydrate_tags_ms += elapsed
                    self.profile.hydrate_tags_sql_ms += sql_ms
                    self.profile.hydrate_tags_decode_ms += decode_ms

        way_ids = list(dict.fromkeys(
            int(r["osm_id"]) for r in rows if r["osm_type"] == "way"
        ))
        if way_mode == "geometry":
            self.preload_way_geometry(way_ids, phase=phase)
        elif way_mode == "ids":
            missing = [wid for wid in way_ids if wid not in self.way_nodes]
            if missing:
                started = time.perf_counter()
                loaded_nodes, sql_ms, decode_ms = _load_way_ids_profiled(
                    self.con, missing, use_packed=self.use_packed_way_ids
                )
                self.way_nodes.update(loaded_nodes)
                elapsed = (time.perf_counter() - started) * 1000.0
                if self.profile is not None:
                    if self.use_packed_way_ids:
                        self.profile.packed_way_ids_calls += 1
                    if phase == "hydrate":
                        self.profile.hydrate_way_ms += elapsed
                        self.profile.hydrate_way_sql_ms += sql_ms
                        self.profile.hydrate_way_decode_ms += decode_ms
        elif way_mode != "none":
            raise ValueError(f"unknown way preload mode: {way_mode}")

        if include_relations:
            relation_ids = [
                int(r["osm_id"]) for r in rows
                if r["osm_type"] == "relation" and int(r["osm_id"]) not in self.relation_members
            ]
            if relation_ids:
                started = time.perf_counter()
                self.relation_members.update(get_relation_members_many(self.con, relation_ids))
                elapsed = (time.perf_counter() - started) * 1000.0
                if self.profile is not None and phase == "hydrate":
                    self.profile.hydrate_relations_ms += elapsed

    def preload_way_geometry(self, way_ids: list[int], *, phase: str = "hydrate") -> None:
        ids = list(dict.fromkeys(int(v) for v in way_ids))
        missing = [wid for wid in ids if wid not in self.way_geometry]
        if not missing:
            return
        started = time.perf_counter()
        loaded, sql_ms, decode_ms = _load_way_geometry_profiled(self.con, missing)
        for wid in missing:
            geometry = loaded.get(wid, [])
            self.way_geometry[wid] = geometry
            # Reuse node IDs from the decoded schema-v5 geometry.
            self.way_nodes[wid] = [node[0] for node in geometry]
        elapsed = (time.perf_counter() - started) * 1000.0
        if self.profile is not None and phase == "hydrate":
            self.profile.hydrate_way_ms += elapsed
            self.profile.hydrate_way_sql_ms += sql_ms
            self.profile.hydrate_way_decode_ms += decode_ms

    def geometries_for_ways(
        self, way_ids: list[int], *, phase: str = "recurse"
    ) -> dict[int, list[tuple[int, float | None, float | None]]]:
        ids = list(dict.fromkeys(int(v) for v in way_ids))
        self.preload_way_geometry(ids, phase=phase)
        return {wid: self.way_geometry.get(wid, []) for wid in ids}

    def tags_for(self, row: sqlite3.Row) -> dict[str, str]:
        pk = int(row["pk"])
        tags = self.tags.get(pk)
        if tags is None:
            tags = get_tags(self.con, pk)
            self.tags[pk] = tags
        return tags

    def nodes_for_way(self, way_id: int) -> list[int]:
        nodes = self.way_nodes.get(int(way_id))
        if nodes is None:
            nodes = get_way_node_ids(self.con, int(way_id))
            self.way_nodes[int(way_id)] = nodes
        return nodes

    def members_for_relation(self, relation_id: int) -> list[dict]:
        members = self.relation_members.get(int(relation_id))
        if members is None:
            members = _relation_members(self.con, int(relation_id))
            self.relation_members[int(relation_id)] = members
        return members


def _to_element(
    con: sqlite3.Connection,
    row: sqlite3.Row,
    requested_kind: str | None = None,
    loader: _ElementLoader | None = None,
) -> dict:
    tags = loader.tags_for(row) if loader is not None else get_tags(con, int(row["pk"]))
    osm_type = row["osm_type"]
    osm_id = int(row["osm_id"])

    if requested_kind == "area":
        return {
            "type": "area",
            "id": _area_id(osm_type, osm_id),
            "center": {"lat": row["center_lat"], "lon": row["center_lon"]},
            "tags": tags,
        }
    if osm_type == "node":
        return {
            "type": "node",
            "id": osm_id,
            "lat": row["center_lat"],
            "lon": row["center_lon"],
            "tags": tags,
        }
    if osm_type == "way":
        return {
            "type": "way",
            "id": osm_id,
            "nodes": loader.nodes_for_way(osm_id) if loader is not None else _way_node_ids(con, osm_id),
            "center": {"lat": row["center_lat"], "lon": row["center_lon"]},
            "tags": tags,
        }
    if osm_type == "relation":
        return {
            "type": "relation",
            "id": osm_id,
            "members": loader.members_for_relation(osm_id) if loader is not None else _relation_members(con, osm_id),
            "center": {"lat": row["center_lat"], "lon": row["center_lon"]},
            "tags": tags,
        }
    raise UnsupportedQuery(f"unsupported stored osm type: {osm_type}")


def _row_by_type_id(con: sqlite3.Connection, osm_type: str, osm_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM objects WHERE osm_type=? AND osm_id=?", (osm_type, osm_id)
    ).fetchone()


def _rows_by_type_ids(
    con: sqlite3.Connection, osm_type: str, osm_ids: list[int]
) -> dict[int, sqlite3.Row]:
    ids = list(dict.fromkeys(int(v) for v in osm_ids))
    out: dict[int, sqlite3.Row] = {}
    chunk_size = sqlite_in_chunk_size(con, fixed_params=1)
    for offset in range(0, len(ids), chunk_size):
        chunk = ids[offset:offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        for row in con.execute(
            f"SELECT * FROM objects WHERE osm_type=? AND osm_id IN ({placeholders})",
            [osm_type, *chunk],
        ):
            out[int(row["osm_id"])] = row
    return out


def _node_element_by_id(
    con: sqlite3.Connection,
    node_id: int,
    coord: tuple[float | None, float | None] | None = None,
    loader: _ElementLoader | None = None,
) -> dict | None:
    row = _row_by_type_id(con, "node", node_id)
    if row is not None:
        return _to_element(con, row, loader=loader)
    if coord is not None and coord[0] is not None and coord[1] is not None:
        lat, lon = float(coord[0]), float(coord[1])
    else:
        stored = get_node_coord(con, node_id)
        if stored is None:
            return None
        lat, lon = stored
    return {"type": "node", "id": int(node_id), "lat": lat, "lon": lon, "tags": {}}


class _SeenElements:
    """Deduplicate API elements without allocating a ``(type, id)`` tuple.

    Large recurse responses can contain well over 100k elements. Separate ID
    sets keep the exact type-aware semantics while avoiding one short-lived
    tuple allocation and one mixed-type tuple hash per candidate element.
    """

    __slots__ = ("nodes", "ways", "relations", "areas", "other")

    def __init__(self) -> None:
        self.nodes: set[int] = set()
        self.ways: set[int] = set()
        self.relations: set[int] = set()
        self.areas: set[int] = set()
        self.other: dict[str, set[int]] = {}

    def add_if_new(self, element_type: str, element_id: int) -> bool:
        eid = int(element_id)
        if element_type == "node":
            bucket = self.nodes
        elif element_type == "way":
            bucket = self.ways
        elif element_type == "relation":
            bucket = self.relations
        elif element_type == "area":
            bucket = self.areas
        else:
            bucket = self.other.setdefault(str(element_type), set())
        if eid in bucket:
            return False
        bucket.add(eid)
        return True


def _append_element(out: list[dict], seen: _SeenElements, element: dict) -> None:
    if seen.add_if_new(str(element["type"]), int(element["id"])):
        out.append(element)


def _append_descendants(
    con: sqlite3.Connection,
    seed_rows: list[sqlite3.Row],
    out: list[dict],
    seen: _SeenElements,
    loader: _ElementLoader | None = None,
    profile: QueryProfile | None = None,
) -> None:
    """Approximate Overpass `(._;>;);` using batched topology hydration.

    Relations are traversed recursively to discover member ways/relations. All
    resulting ways are then expanded in one batched topology read, and member
    node objects/tags are looked up in chunks instead of one SQL query per node.
    """
    if loader is None:
        loader = _ElementLoader(con, profile=profile)

    way_order: list[int] = []
    way_seen: set[int] = set()
    relation_queue: list[int] = []
    relation_seen: set[int] = set()

    for row in seed_rows:
        osm_type = str(row["osm_type"])
        osm_id = int(row["osm_id"])
        if osm_type == "way" and osm_id not in way_seen:
            way_seen.add(osm_id)
            way_order.append(osm_id)
        elif osm_type == "relation" and osm_id not in relation_seen:
            relation_queue.append(osm_id)

    # Relation graphs are normally shallow. Process one relation at a time but
    # batch-load all of that relation's referenced stored objects together.
    relations_started = time.perf_counter()
    qpos = 0
    while qpos < len(relation_queue):
        relation_id = relation_queue[qpos]
        qpos += 1
        if relation_id in relation_seen:
            continue
        relation_seen.add(relation_id)
        members = loader.members_for_relation(relation_id)
        refs: dict[str, list[int]] = {"node": [], "way": [], "relation": []}
        for member in members:
            mtype = str(member["type"])
            if mtype in refs:
                refs[mtype].append(int(member["ref"]))

        rows_by_type = {
            mtype: _rows_by_type_ids(con, mtype, ids)
            for mtype, ids in refs.items() if ids
        }
        member_rows = [row for mapping in rows_by_type.values() for row in mapping.values()]
        loader.preload(member_rows, way_mode="geometry", phase="recurse")

        for member in members:
            mtype, mid = str(member["type"]), int(member["ref"])
            row = rows_by_type.get(mtype, {}).get(mid)
            if row is None:
                # Preserve previous behavior for relation-member nodes that are
                # coordinate dependencies but not standalone stored OSM objects.
                continue
            _append_element(out, seen, _to_element(con, row, loader=loader))
            if mtype == "way" and mid not in way_seen:
                way_seen.add(mid)
                way_order.append(mid)
            elif mtype == "relation" and mid not in relation_seen:
                relation_queue.append(mid)

    if profile is not None:
        profile.recurse_relations_ms += (time.perf_counter() - relations_started) * 1000.0

    if not way_order:
        return

    coordmap_started = time.perf_counter()
    nodes_by_way = loader.geometries_for_ways(way_order, phase="recurse")
    # Preserve first-seen node order while retaining one coordinate per node.
    coord_by_node: dict[int, tuple[float | None, float | None]] = {}
    for way_id in way_order:
        for node_id, lat, lon in nodes_by_way.get(way_id, []):
            nid = int(node_id)
            previous = coord_by_node.get(nid)
            if previous is None:
                coord_by_node[nid] = (lat, lon)
            elif previous[0] is None and lat is not None:
                coord_by_node[nid] = (lat, lon)

    ordered_node_ids = list(coord_by_node)
    if profile is not None:
        profile.recurse_coordmap_ms += (time.perf_counter() - coordmap_started) * 1000.0

    # Probe only this request's descendants. Building a global node-membership
    # set can scan every object on schema v6 and retain millions of Python IDs
    # per reader. Missing standalone rows still use embedded coordinates below.
    node_sql_started = time.perf_counter()
    node_rows = _rows_by_type_ids(con, "node", ordered_node_ids)
    loader.preload(list(node_rows.values()), phase="recurse")
    if profile is not None:
        profile.recurse_node_sql_ms += (time.perf_counter() - node_sql_started) * 1000.0

    emit_started = time.perf_counter()
    # ``ordered_node_ids`` is already unique by construction. Special-case node
    # emission so the 100k+ element hot path does not repeatedly dispatch through
    # generic type-aware deduplication or re-read the coordinate dictionary.
    seen_nodes = seen.nodes
    append_out = out.append
    for node_id, coord in coord_by_node.items():
        if node_id in seen_nodes:
            continue
        row = node_rows.get(node_id)
        if row is not None:
            seen_nodes.add(node_id)
            append_out(_to_element(con, row, loader=loader))
            continue
        lat, lon = coord
        if lat is not None and lon is not None:
            seen_nodes.add(node_id)
            append_out({
                "type": "node",
                "id": node_id,
                "lat": float(lat),
                "lon": float(lon),
                "tags": {},
            })
    if profile is not None:
        profile.recurse_emit_ms += (time.perf_counter() - emit_started) * 1000.0

def _way_nodes_for_ids(
    con: sqlite3.Connection, way_ids: list[int]
) -> dict[int, tuple[float | None, float | None]]:
    """Fetch node IDs and any embedded coordinates across schema v2-v4."""
    return get_way_nodes_many(con, way_ids)


@lru_cache(maxsize=32)
def _has_schema5_way_geometry(con: sqlite3.Connection) -> bool:
    try:
        return con.execute("SELECT 1 FROM way_geometry_v5 LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


@lru_cache(maxsize=32)
def _has_schema4_way_geometry(con: sqlite3.Connection) -> bool:
    try:
        return con.execute("SELECT 1 FROM way_geometry LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _road_intersection_elements(
    con: sqlite3.Connection,
    node_ids: list[int],
    coord_by_node: dict[int, tuple[float | None, float | None]],
    profile: QueryProfile | None,
) -> list[dict]:
    """Hydrate intersection nodes in batches instead of one SELECT per node."""
    hydrate_started = time.perf_counter()
    node_rows = _rows_by_type_ids(con, "node", node_ids)
    loader = _ElementLoader(con)
    loader.preload(list(node_rows.values()))
    if profile is not None:
        profile.road_hydrate_ms += (time.perf_counter() - hydrate_started) * 1000.0

    output_started = time.perf_counter()
    out: list[dict] = []
    for node_id in node_ids:
        row = node_rows.get(node_id)
        if row is not None:
            out.append(_to_element(con, row, loader=loader))
            continue
        coord = coord_by_node.get(node_id)
        if coord is None or coord[0] is None or coord[1] is None:
            stored = get_node_coord(con, node_id)
            if stored is None:
                continue
            coord = stored
        out.append({
            "type": "node",
            "id": int(node_id),
            "lat": float(coord[0]),
            "lon": float(coord[1]),
            "tags": {},
        })
    if profile is not None:
        profile.road_output_ms += (time.perf_counter() - output_started) * 1000.0
    return out


def _road_intersections_packed(
    con: sqlite3.Connection,
    major_sel: Selector,
    minor_sel: Selector,
    profile: QueryProfile | None,
) -> list[dict] | None:
    """Resolve road intersections with one spatially seeded topology pass."""
    has_v5 = _has_schema5_way_geometry(con)
    has_v4 = _has_schema4_way_geometry(con)
    if not has_v5 and not has_v4:
        return None
    if (
        major_sel.south, major_sel.west, major_sel.north, major_sel.east,
        major_sel.spatial_mode, major_sel.center_lat, major_sel.center_lon, major_sel.radius_m,
    ) != (
        minor_sel.south, minor_sel.west, minor_sel.north, minor_sel.east,
        minor_sel.spatial_mode, minor_sel.center_lat, minor_sel.center_lon, minor_sel.radius_m,
    ):
        return None

    def highway_values(sel: Selector) -> tuple[str, ...] | None:
        regex_filters = [f for f in sel.filters if f.key == "highway" and f.op == "~"]
        if len(regex_filters) != 1 or len(sel.filters) != 1:
            return None
        return _regex_matching_values(con, "highway", regex_filters[0].value or "")

    resolve_started = time.perf_counter()
    major_values = highway_values(major_sel)
    minor_values = highway_values(minor_sel)
    if profile is not None:
        profile.candidate_sql_ms += (time.perf_counter() - resolve_started) * 1000.0
    if major_values is None or minor_values is None:
        return None
    major_set = set(major_values)
    minor_set = set(minor_values)
    all_values = tuple(sorted(major_set | minor_set))
    if not all_values:
        return []

    # Preferred schema-v4 path: optional compact road-only RTree. It contains
    # only R&E-relevant road ways and numeric class codes, so the query avoids
    # the generic objects/object_tags joins entirely. Databases without the
    # derived index transparently fall back to the validated generic path.
    if road_index_ready(con):
        class_codes = tuple(sorted(ROAD_CLASS_BY_VALUE[v] for v in all_values if v in ROAD_CLASS_BY_VALUE))
        if len(class_codes) == len(all_values):
            placeholders = ",".join("?" for _ in class_codes)
            fast_road_nodes = has_v5 and road_index_fast_nodes_ready(con)
            if fast_road_nodes:
                # Road-index v2 stores a fixed-width node-ID stream directly on
                # the indexed road row. This removes the geometry-table join from
                # large candidate scans and lets Python iterate IDs without
                # varint decoding. The authoritative Schema-5 geometry remains
                # unchanged and is still used later for coordinates.
                indexed_sql = f"""
                    SELECT rw.way_id AS way_id, rw.road_class AS road_class,
                           rw.node_ids_fixed AS node_ids
                    FROM road_way_rtree rr
                    CROSS JOIN road_ways rw ON rw.way_id=rr.way_id
                    WHERE rr.min_lon <= ? AND rr.max_lon >= ?
                      AND rr.min_lat <= ? AND rr.max_lat >= ?
                      AND rw.road_class IN ({placeholders})
                """
            elif has_v5:
                # Schema 5 deliberately splits topology IDs from coordinates.
                # Road-intersection discovery needs only node membership, so do
                # not drag/decode the coordinate stream for every candidate way.
                # Large R&E fallback radii can cover hundreds of thousands of
                # roads; coordinates are fetched later only for ways that actually
                # contribute a returned intersection node.
                indexed_sql = f"""
                    SELECT rw.way_id AS way_id, rw.road_class AS road_class,
                           g.node_ids AS node_ids
                    FROM road_way_rtree rr
                    CROSS JOIN road_ways rw ON rw.way_id=rr.way_id
                    CROSS JOIN way_geometry_v5 g ON g.way_id=rw.way_id
                    WHERE rr.min_lon <= ? AND rr.max_lon >= ?
                      AND rr.min_lat <= ? AND rr.max_lat >= ?
                      AND rw.road_class IN ({placeholders})
                """
            else:
                indexed_sql = f"""
                    SELECT rw.way_id AS way_id, rw.road_class AS road_class,
                           g.node_data AS node_data
                    FROM road_way_rtree rr
                    CROSS JOIN road_ways rw ON rw.way_id=rr.way_id
                    CROSS JOIN way_geometry g ON g.way_id=rw.way_id
                    WHERE rr.min_lon <= ? AND rr.max_lon >= ?
                      AND rr.min_lat <= ? AND rr.max_lat >= ?
                      AND rw.road_class IN ({placeholders})
                """
            indexed_params: list[object] = [
                major_sel.east, major_sel.west, major_sel.north, major_sel.south,
                *class_codes,
            ]
            sql_started = time.perf_counter()
            indexed_rows = list(con.execute(indexed_sql, indexed_params))
            if profile is not None:
                profile.candidate_sql_ms += (time.perf_counter() - sql_started) * 1000.0
                profile.candidate_calls += 1
                profile.spatial_seeded_calls += 1
                profile.road_index_calls += 1
                profile.candidate_rows += len(indexed_rows)
                profile.matched_rows += len(indexed_rows)

            topology_started = time.perf_counter()
            major_codes = {ROAD_CLASS_BY_VALUE[v] for v in major_set if v in ROAD_CLASS_BY_VALUE}
            minor_codes = {ROAD_CLASS_BY_VALUE[v] for v in minor_set if v in ROAD_CLASS_BY_VALUE}

            if has_v5 and major_codes.isdisjoint(minor_codes):
                node_id_reader = fixed_node_ids_view if fast_road_nodes else decode_way_node_ids
                # R&E's two known road-intersection grammars have disjoint
                # major/minor classes. Exploit that property to avoid a giant
                # node_id -> [mask,lat,lon] dictionary and, crucially, avoid
                # decoding coordinates for every road in a 150 km fallback.
                major_nodes: set[int] = set()
                for row in indexed_rows:
                    if int(row["road_class"]) in major_codes:
                        major_nodes.update(node_id_reader(row["node_ids"]))

                if not major_nodes:
                    if profile is not None:
                        profile.road_topology_ms += (time.perf_counter() - topology_started) * 1000.0
                    return []

                intersection_set: set[int] = set()
                # Store only one source way for each newly discovered intersection.
                # Its coordinate stream is fetched in a second, small batch.
                needed_by_way: dict[int, set[int]] = {}
                for row in indexed_rows:
                    if int(row["road_class"]) not in minor_codes:
                        continue
                    way_id = int(row["way_id"])
                    for node_id in node_id_reader(row["node_ids"]):
                        node_id = int(node_id)
                        if node_id not in major_nodes or node_id in intersection_set:
                            continue
                        intersection_set.add(node_id)
                        needed_by_way.setdefault(way_id, set()).add(node_id)

                intersection_ids = sorted(intersection_set)
                coord_by_node: dict[int, tuple[float | None, float | None]] = {}

                if needed_by_way:
                    source_way_ids = list(needed_by_way)
                    chunk_size = sqlite_in_chunk_size(con)
                    for offset in range(0, len(source_way_ids), chunk_size):
                        chunk = source_way_ids[offset:offset + chunk_size]
                        placeholders2 = ",".join("?" for _ in chunk)
                        for grow in con.execute(
                            f"SELECT way_id,node_ids,coordinates FROM way_geometry_v5 "
                            f"WHERE way_id IN ({placeholders2})",
                            chunk,
                        ):
                            way_id = int(grow["way_id"])
                            needed = needed_by_way.get(way_id)
                            if not needed:
                                continue
                            for node_id, lat, lon in decode_way_geometry_v5(
                                grow["node_ids"], grow["coordinates"]
                            ):
                                node_id = int(node_id)
                                if node_id in needed:
                                    coord_by_node[node_id] = (lat, lon)

                if profile is not None:
                    profile.road_topology_ms += (time.perf_counter() - topology_started) * 1000.0
                return _road_intersection_elements(
                    con, intersection_ids, coord_by_node, profile
                )

            # Schema 4 and unusual overlapping class sets keep the established
            # one-pass geometry path for compatibility.
            node_state: dict[int, list[float | int | None]] = {}
            for row in indexed_rows:
                road_class = int(row["road_class"])
                bit = (1 if road_class in major_codes else 0) | (2 if road_class in minor_codes else 0)
                if not bit:
                    continue
                geometry = decode_way_geometry(row["node_data"])
                for node_id, lat, lon in geometry:
                    node_id = int(node_id)
                    state = node_state.get(node_id)
                    if state is None:
                        node_state[node_id] = [bit, lat, lon]
                    else:
                        state[0] = int(state[0]) | bit
                        if state[1] is None and lat is not None:
                            state[1], state[2] = lat, lon
            intersection_ids = sorted(
                node_id for node_id, state in node_state.items() if int(state[0]) == 3
            )
            coord_by_node = {
                node_id: (node_state[node_id][1], node_state[node_id][2])
                for node_id in intersection_ids
            }
            if profile is not None:
                profile.road_topology_ms += (time.perf_counter() - topology_started) * 1000.0
            return _road_intersection_elements(con, intersection_ids, coord_by_node, profile)

    # Generic fallback uses the object RTree as the spatial seed.
    placeholders = ",".join("?" for _ in all_values)
    if has_v5:
        sql = f"""
            SELECT o.osm_id AS way_id, h.value AS highway_value,
                   g.node_ids AS node_ids, g.coordinates AS coordinates
            FROM object_rtree r
            CROSS JOIN objects o ON o.pk=r.pk
            JOIN object_tags h ON h.object_pk=o.pk AND h.key='highway'
            JOIN way_geometry_v5 g ON g.way_id=o.osm_id
            WHERE r.min_lon <= ? AND r.max_lon >= ?
              AND r.min_lat <= ? AND r.max_lat >= ?
              AND o.osm_type='way'
              AND h.value IN ({placeholders})
        """
    else:
        sql = f"""
            SELECT o.osm_id AS way_id, h.value AS highway_value, g.node_data AS node_data
            FROM object_rtree r
            CROSS JOIN objects o ON o.pk=r.pk
            JOIN object_tags h ON h.object_pk=o.pk AND h.key='highway'
            JOIN way_geometry g ON g.way_id=o.osm_id
            WHERE r.min_lon <= ? AND r.max_lon >= ?
              AND r.min_lat <= ? AND r.max_lat >= ?
              AND o.osm_type='way'
              AND h.value IN ({placeholders})
        """
    params: list[object] = [
        major_sel.east, major_sel.west, major_sel.north, major_sel.south,
        *all_values,
    ]
    sql_started = time.perf_counter()
    rows = list(con.execute(sql, params))
    if profile is not None:
        profile.candidate_sql_ms += (time.perf_counter() - sql_started) * 1000.0
        profile.candidate_calls += 1
        profile.spatial_seeded_calls += 1
        profile.candidate_rows += len(rows)
        profile.matched_rows += len(rows)

    topology_started = time.perf_counter()
    # state: node_id -> [major/minor mask, lat, lon]. Lists avoid allocating a
    # replacement tuple for every OR operation across millions of references.
    node_state: dict[int, list[float | int | None]] = {}
    for row in rows:
        value = str(row["highway_value"])
        bit = (1 if value in major_set else 0) | (2 if value in minor_set else 0)
        if not bit:
            continue
        geometry = (
            decode_way_geometry_v5(row["node_ids"], row["coordinates"])
            if has_v5
            else decode_way_geometry(row["node_data"])
        )
        for node_id, lat, lon in geometry:
            state = node_state.get(int(node_id))
            if state is None:
                node_state[int(node_id)] = [bit, lat, lon]
            else:
                state[0] = int(state[0]) | bit
                if state[1] is None and lat is not None:
                    state[1], state[2] = lat, lon

    intersection_ids = sorted(
        node_id for node_id, state in node_state.items() if int(state[0]) == 3
    )
    coord_by_node = {
        node_id: (node_state[node_id][1], node_state[node_id][2])
        for node_id in intersection_ids
    }
    if profile is not None:
        profile.road_topology_ms += (time.perf_counter() - topology_started) * 1000.0
    return _road_intersection_elements(con, intersection_ids, coord_by_node, profile)


def _road_intersections(
    con: sqlite3.Connection,
    selectors: list[Selector],
    profile: QueryProfile | None = None,
) -> list[dict]:
    way_selectors = [
        s
        for s in selectors
        if s.kind == "way"
        and any(f.key == "highway" and f.op == "~" for f in s.filters)
    ]
    if len(way_selectors) < 2:
        raise UnsupportedQuery("road-intersection query did not contain both highway selectors")

    fast = _road_intersections_packed(con, way_selectors[0], way_selectors[1], profile)
    if fast is not None:
        return fast

    # Schema-v2/v3 compatibility fallback. Candidate selection remains generic,
    # but intersection-node hydration is batched to avoid the historical N+1.
    major_rows = _candidate_rows(con, way_selectors[0], profile)
    minor_rows = _candidate_rows(con, way_selectors[1], profile)
    major_ids = [int(r["osm_id"]) for r in major_rows]
    minor_ids = [int(r["osm_id"]) for r in minor_rows]
    if not major_ids or not minor_ids:
        return []

    topology_started = time.perf_counter()
    major_nodes = _way_nodes_for_ids(con, major_ids)
    minor_nodes = _way_nodes_for_ids(con, minor_ids)
    node_ids = sorted(set(major_nodes) & set(minor_nodes))
    coord_by_node: dict[int, tuple[float | None, float | None]] = {}
    for node_id in node_ids:
        coord = major_nodes.get(node_id)
        if coord is None or coord[0] is None or coord[1] is None:
            coord = minor_nodes.get(node_id)
        if coord is not None:
            coord_by_node[node_id] = coord
    if profile is not None:
        profile.road_topology_ms += (time.perf_counter() - topology_started) * 1000.0
    return _road_intersection_elements(con, node_ids, coord_by_node, profile)


def _unwrap_lon(vertex_lon: float, point_lon: float) -> float:
    return point_lon + ((vertex_lon - point_lon + 180.0) % 360.0 - 180.0)


_GEO_LAT_BIN_DEG = 0.10


def _lat_bin(lat: float) -> int:
    return math.floor((float(lat) + 90.0) / _GEO_LAT_BIN_DEG)


def _prepare_ring(ring: list) -> tuple[
    tuple[float, float, float, float, bool],
    dict[int, tuple[tuple[float, float, float, float], ...]],
]:
    """Prepare one polygon ring for fast repeated ray-casting.

    A point-in-ring test only needs edges whose latitude span crosses the query
    latitude. Bucket those edges at 0.1-degree resolution once, so Finland's
    very detailed coastline does not require scanning every coastline vertex on
    every ``is_in()`` call.
    """
    if len(ring) < 4:
        return ((0.0, 0.0, 0.0, 0.0, False), {})
    lons = [float(pt[0]) for pt in ring]
    lats = [float(pt[1]) for pt in ring]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    crosses_dateline = (max_lon - min_lon) > 180.0
    bins: dict[int, list[tuple[float, float, float, float]]] = {}
    prev = ring[-1]
    xj, yj = float(prev[0]), float(prev[1])
    for pt in ring:
        xi, yi = float(pt[0]), float(pt[1])
        if yi != yj:  # horizontal edges never change ray-casting parity
            lo, hi = sorted((yi, yj))
            b0, b1 = _lat_bin(lo), _lat_bin(hi)
            edge = (xi, yi, xj, yj)
            for b in range(b0, b1 + 1):
                bins.setdefault(b, []).append(edge)
        xj, yj = xi, yi
    return (
        (min_lon, max_lon, min_lat, max_lat, crosses_dateline),
        {b: tuple(edges) for b, edges in bins.items()},
    )


def _point_in_prepared_ring(
    lon: float,
    lat: float,
    prepared: tuple[
        tuple[float, float, float, float, bool],
        dict[int, tuple[tuple[float, float, float, float], ...]],
    ],
) -> bool:
    (min_lon, max_lon, min_lat, max_lat, crosses_dateline), bins = prepared
    if lat < min_lat or lat > max_lat:
        return False
    if not crosses_dateline and (lon < min_lon or lon > max_lon):
        return False
    inside = False
    for xi0, yi, xj0, yj in bins.get(_lat_bin(lat), ()):
        # Bucket membership is conservative; exact crossing semantics are unchanged.
        if (yi > lat) == (yj > lat):
            continue
        xi = _unwrap_lon(xi0, lon)
        xj = _unwrap_lon(xj0, lon)
        x_at_y = (xj - xi) * (lat - yi) / (yj - yi) + xi
        if lon < x_at_y:
            inside = not inside
    return inside


def _prepare_geojson_text(geojson_text: str) -> tuple:
    try:
        geom = __import__('json').loads(geojson_text)
    except Exception:
        return ()
    gtype = geom.get('type')
    coords = geom.get('coordinates') or []
    polygons = [coords] if gtype == 'Polygon' else coords if gtype == 'MultiPolygon' else []
    prepared: list[tuple[object, tuple[object, ...]]] = []
    for poly in polygons:
        if not poly or not poly[0]:
            continue
        outer = _prepare_ring(poly[0])
        holes = tuple(_prepare_ring(hole) for hole in poly[1:] if hole)
        prepared.append((outer, holes))
    return tuple(prepared)


@lru_cache(maxsize=64)
def _prepared_area_geometry(con: sqlite3.Connection, object_pk: int) -> tuple | None:
    """Load and prepare a country polygon once per persistent DB connection."""
    geometry = get_area_geometry(con, int(object_pk))
    if geometry is None:
        return None
    return _prepare_geojson_text(geometry)


def _point_in_prepared_geojson(lon: float, lat: float, prepared: tuple) -> bool:
    for outer, holes in prepared:
        if not _point_in_prepared_ring(lon, lat, outer):
            continue
        if any(_point_in_prepared_ring(lon, lat, hole) for hole in holes):
            continue
        return True
    return False


def _is_in_pivot_relations(
    con: sqlite3.Connection,
    query: str,
    profile: QueryProfile | None = None,
) -> list[dict] | None:
    """Handle HPG's runtime country lookup with indexed exact geometry tests."""
    parse_started = time.perf_counter()
    m = IS_IN_PIVOT_RE.search(query)
    if not m:
        return None
    if m.group("set").lower() != m.group("pivot_set").lower():
        raise UnsupportedQuery("is_in result set and rel(pivot.*) set do not match")

    lat = float(m.group("lat"))
    lon = float(m.group("lon"))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise UnsupportedQuery("is_in coordinate is outside valid latitude/longitude range")

    filters = _parse_filter_blocks(m.group("filters") or "")
    if profile is not None:
        profile.parse_ms += (time.perf_counter() - parse_started) * 1000.0
    where = [
        "o.osm_type='relation'",
        "r.min_lon <= ?", "r.max_lon >= ?",
        "r.min_lat <= ?", "r.max_lat >= ?",
    ]
    params: list[object] = [lon, lon, lat, lat]
    for i, flt in enumerate(filters):
        if flt.op == "=":
            where.append(
                f"EXISTS (SELECT 1 FROM object_tags t{i} "
                f"WHERE t{i}.object_pk=o.pk AND t{i}.key=? AND t{i}.value=?)"
            )
            params.extend([flt.key, flt.value or ""])
        elif flt.op == "exists":
            where.append(
                f"EXISTS (SELECT 1 FROM object_tags t{i} "
                f"WHERE t{i}.object_pk=o.pk AND t{i}.key=?)"
            )
            params.append(flt.key)
        elif flt.op == "!=":
            where.append(
                f"NOT EXISTS (SELECT 1 FROM object_tags t{i} "
                f"WHERE t{i}.object_pk=o.pk AND t{i}.key=? AND t{i}.value=?)"
            )
            params.extend([flt.key, flt.value or ""])
        else:
            # HPG's country lookup currently uses exact boundary/admin_level
            # filters. Preserve regex support via the existing Python matcher if
            # another runtime version introduces it.
            pass

    sql_started = time.perf_counter()
    rows = list(con.execute(
        "SELECT o.* FROM object_rtree r JOIN objects o ON o.pk=r.pk WHERE "
        + " AND ".join(where),
        params,
    ))
    if profile is not None:
        profile.is_in_sql_ms += (time.perf_counter() - sql_started) * 1000.0

    hydrate_started = time.perf_counter()
    tags_by_pk = get_tags_many(con, [int(row["pk"]) for row in rows])
    if profile is not None:
        profile.is_in_hydrate_ms += (time.perf_counter() - hydrate_started) * 1000.0

    geometry_started = time.perf_counter()
    matched: list[tuple[sqlite3.Row, dict[str, str]]] = []
    for row in rows:
        tags = tags_by_pk.get(int(row["pk"]), {})
        # Only regex filters were intentionally left outside SQL above.
        if filters and not all(_filter_matches(tags, flt) for flt in filters):
            continue
        prepared = _prepared_area_geometry(con, int(row["pk"]))
        # The RTree bbox is only a candidate prefilter. Overpass is_in()
        # semantics require exact geometric containment, so a relation without
        # stored area geometry must never be accepted from its bbox alone.
        if prepared is None:
            continue
        if not _point_in_prepared_geojson(lon, lat, prepared):
            continue
        matched.append((row, tags))
    if profile is not None:
        profile.is_in_geometry_ms += (time.perf_counter() - geometry_started) * 1000.0

    output_started = time.perf_counter()
    result = [
        {"type": "relation", "id": int(row["osm_id"]), "tags": tags}
        for row, tags in matched
    ]
    if profile is not None:
        profile.is_in_output_ms += (time.perf_counter() - output_started) * 1000.0
    return result


def execute_overpass_subset(
    con: sqlite3.Connection,
    query: str,
    profile: QueryProfile | None = None,
) -> list[dict]:
    query = query.strip()
    if "{{bbox}}" in query:
        raise UnsupportedQuery(
            "query still contains {{bbox}}; R&E/HPG must substitute a numeric bounding box before HTTP dispatch"
        )
    if "[out:json]" not in query.replace(" ", "").lower():
        raise UnsupportedQuery("only [out:json] R&E queries are supported")

    is_in_result = _is_in_pivot_relations(con, query, profile)
    if is_in_result is not None:
        return is_in_result

    parse_started = time.perf_counter()
    selectors = parse_selectors(query)
    if not selectors:
        raise UnsupportedQuery("no supported node/way/area/nwr bbox or around selectors found")

    compact = re.sub(r"\s+", "", query)
    if profile is not None:
        profile.parse_ms += (time.perf_counter() - parse_started) * 1000.0
    if ROAD_INTERSECTION_MARKER in compact:
        return _road_intersections(con, selectors, profile)

    seed_rows: list[sqlite3.Row] = []
    selected: list[tuple[sqlite3.Row, str | None]] = []
    for sel in selectors:
        rows = _candidate_rows(con, sel, profile)
        for row in rows:
            seed_rows.append(row)
            selected.append((row, "area" if sel.kind == "area" else None))

    # Hydrate result payloads in batches.
    loader = _ElementLoader(con, profile=profile)
    unique_rows: list[sqlite3.Row] = []
    seen_pk: set[int] = set()
    for row, _requested_kind in selected:
        pk = int(row["pk"])
        if pk not in seen_pk:
            seen_pk.add(pk)
            unique_rows.append(row)

    recurse_down = RECURSE_DOWN_MARKER_RE.search(query) is not None

    # Tags are needed for every returned object, but topology requirements differ:
    # - area aliases never expose way nodes/relation members themselves;
    # - recursive queries need full schema-4 geometry eventually, so decode it
    #   once here and reuse it during descendant expansion;
    # - ordinary way/relation output only needs ID topology/member lists.
    topology_rows: list[sqlite3.Row] = []
    topology_seen: set[int] = set()
    for row, requested_kind in selected:
        if requested_kind == "area" and not recurse_down:
            continue
        pk = int(row["pk"])
        if pk not in topology_seen:
            topology_seen.add(pk)
            topology_rows.append(row)

    hydrate_started = time.perf_counter()
    loader.preload(unique_rows, way_mode="none", include_relations=False)
    if topology_rows:
        loader.preload(
            topology_rows,
            way_mode="geometry" if recurse_down else "ids",
            include_relations=True,
        )
    if profile is not None:
        profile.hydrate_ms += (time.perf_counter() - hydrate_started) * 1000.0

    output_started = time.perf_counter()
    out: list[dict] = []
    seen = _SeenElements()
    for row, requested_kind in selected:
        _append_element(out, seen, _to_element(con, row, requested_kind, loader=loader))
    if profile is not None:
        profile.output_ms += (time.perf_counter() - output_started) * 1000.0

    if recurse_down:
        recurse_started = time.perf_counter()
        # Areas are a compatibility abstraction; descendant traversal uses the real stored OSM type.
        _append_descendants(con, seed_rows, out, seen, loader=loader, profile=profile)
        if profile is not None:
            profile.recurse_ms += (time.perf_counter() - recurse_started) * 1000.0
    return out
