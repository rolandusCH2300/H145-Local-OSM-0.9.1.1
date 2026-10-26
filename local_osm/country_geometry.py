from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .db import get_area_geometry, get_tags, get_way_nodes_by_way, set_area_geometry


@dataclass(slots=True)
class CountryGeometryRecovery:
    relation_id: int
    status: str
    outer_rings: int = 0
    inner_rings: int = 0
    missing_way_members: int = 0
    unusable_way_members: int = 0
    skipped_open_outer_components: int = 0
    skipped_open_inner_components: int = 0
    orphan_inner_rings: int = 0


@dataclass(slots=True)
class _Segment:
    seq: int
    way_id: int
    nodes: list[tuple[int, float, float]]


@dataclass(slots=True)
class _WayMember:
    seq: int
    way_id: int
    inner: bool


def _normalize_iso2(values: Iterable[str]) -> set[str]:
    return {
        str(value).strip().upper()
        for value in values
        if str(value).strip()
    }


def _country_iso2(tags: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for key in ("ISO3166-1:alpha2", "ISO3166-1"):
        raw = tags.get(key)
        if not raw:
            continue
        for token in str(raw).replace(",", ";").split(";"):
            token = token.strip().upper()
            if len(token) == 2 and token.isalpha():
                out.add(token)
    return out


def _collect_way_members(
    con: sqlite3.Connection,
    relation_id: int,
) -> list[_WayMember]:
    """Flatten geometric way members through nested boundary relations.
    Nested relation roles compose geometrically: inner flips inherited parity,
    while outer or empty roles preserve it. Cycles on the active recursion
    path are ignored.
    """
    members: list[_WayMember] = []
    next_seq = 0

    def walk(rid: int, inherited_inner: bool, stack: set[int]) -> None:
        nonlocal next_seq
        rid = int(rid)
        if rid in stack:
            return
        rows = list(
            con.execute(
                "SELECT seq,member_type,member_id,role FROM relation_members "
                "WHERE relation_id=? ORDER BY seq",
                (rid,),
            )
        )
        child_stack = set(stack)
        child_stack.add(rid)
        for row in rows:
            member_type = str(row["member_type"])
            member_id = int(row["member_id"])
            role = str(row["role"] or "").strip().lower()
            if role not in {"", "outer", "inner"}:
                # label/admin_centre/subarea and other non-geometry roles do not
                # participate in polygon assembly.
                continue
            member_inner = inherited_inner ^ (role == "inner")
            if member_type == "way":
                members.append(_WayMember(next_seq, member_id, member_inner))
                next_seq += 1
            elif member_type == "relation":
                walk(member_id, member_inner, child_stack)

    walk(int(relation_id), False, set())
    return members


def _load_segments(
    con: sqlite3.Connection,
    relation_id: int,
) -> tuple[list[_Segment], list[_Segment], int, int]:
    members = _collect_way_members(con, relation_id)
    way_ids = [member.way_id for member in members]
    geometries = get_way_nodes_by_way(con, way_ids)

    outers: list[_Segment] = []
    inners: list[_Segment] = []
    missing = 0
    unusable = 0
    for member in members:
        raw_nodes = geometries.get(member.way_id) or []
        if not raw_nodes:
            missing += 1
            continue
        if len(raw_nodes) < 2 or any(lat is None or lon is None for _nid, lat, lon in raw_nodes):
            unusable += 1
            continue
        nodes = [(int(nid), float(lat), float(lon)) for nid, lat, lon in raw_nodes]
        segment = _Segment(member.seq, member.way_id, nodes)
        if member.inner:
            inners.append(segment)
        else:
            outers.append(segment)
    return outers, inners, missing, unusable


def _ring_coordinates(nodes: list[tuple[int, float, float]]) -> list[list[float]]:
    return [[float(lon), float(lat)] for _nid, lat, lon in nodes]


def _assemble_closed_rings(segments: list[_Segment]) -> tuple[list[list[list[float]]], int]:
    """Assemble exact rings from relation way members.

    Ways are connected only when their OSM endpoint node IDs match exactly.
    Components with dangling or ambiguous endpoint degree are discarded; no
    coordinate snapping or artificial closing segment is ever introduced.
    """
    rings: list[list[list[float]]] = []
    open_segments: list[_Segment] = []

    for segment in segments:
        if segment.nodes[0][0] == segment.nodes[-1][0]:
            if len(segment.nodes) >= 4:
                rings.append(_ring_coordinates(segment.nodes))
            continue
        open_segments.append(segment)

    if not open_segments:
        return rings, 0

    endpoint_to_segments: dict[int, list[int]] = {}
    for idx, segment in enumerate(open_segments):
        endpoint_to_segments.setdefault(segment.nodes[0][0], []).append(idx)
        endpoint_to_segments.setdefault(segment.nodes[-1][0], []).append(idx)

    remaining = set(range(len(open_segments)))
    skipped_components = 0

    while remaining:
        seed = next(iter(remaining))
        component: set[int] = set()
        stack = [seed]
        while stack:
            idx = stack.pop()
            if idx in component:
                continue
            component.add(idx)
            segment = open_segments[idx]
            for endpoint in (segment.nodes[0][0], segment.nodes[-1][0]):
                for neighbor in endpoint_to_segments.get(endpoint, ()):
                    if neighbor not in component:
                        stack.append(neighbor)
        remaining.difference_update(component)

        endpoint_degree: dict[int, int] = {}
        for idx in component:
            segment = open_segments[idx]
            endpoint_degree[segment.nodes[0][0]] = endpoint_degree.get(segment.nodes[0][0], 0) + 1
            endpoint_degree[segment.nodes[-1][0]] = endpoint_degree.get(segment.nodes[-1][0], 0) + 1

        # A simple exact ring component has degree two at every join. Degree one
        # means a missing boundary member; >2 is ambiguous and is also rejected.
        if any(degree != 2 for degree in endpoint_degree.values()):
            skipped_components += 1
            continue

        start_idx = min(component, key=lambda idx: open_segments[idx].seq)
        start_segment = open_segments[start_idx]
        ring_nodes = list(start_segment.nodes)
        used = {start_idx}
        start_node = ring_nodes[0][0]
        current_node = ring_nodes[-1][0]
        failed = False

        while len(used) < len(component):
            candidates = [
                idx
                for idx in endpoint_to_segments.get(current_node, ())
                if idx in component and idx not in used
            ]
            if len(candidates) != 1:
                failed = True
                break
            next_idx = candidates[0]
            next_nodes = open_segments[next_idx].nodes
            if next_nodes[0][0] == current_node:
                oriented = next_nodes
            elif next_nodes[-1][0] == current_node:
                oriented = list(reversed(next_nodes))
            else:
                failed = True
                break
            ring_nodes.extend(oriented[1:])
            used.add(next_idx)
            current_node = ring_nodes[-1][0]

        if (
            failed
            or len(used) != len(component)
            or current_node != start_node
            or len(ring_nodes) < 4
        ):
            skipped_components += 1
            continue

        rings.append(_ring_coordinates(ring_nodes))

    return rings, skipped_components


def _unwrap_lon(vertex_lon: float, point_lon: float) -> float:
    return point_lon + ((vertex_lon - point_lon + 180.0) % 360.0 - 180.0)


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    if len(ring) < 4:
        return False
    xj0, yj = float(ring[-1][0]), float(ring[-1][1])
    for point in ring:
        xi0, yi = float(point[0]), float(point[1])
        if (yi > lat) != (yj > lat):
            xi = _unwrap_lon(xi0, lon)
            xj = _unwrap_lon(xj0, lon)
            x_at_y = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_at_y:
                inside = not inside
        xj0, yj = xi0, yi
    return inside


def _ring_area(ring: list[list[float]]) -> float:
    if len(ring) < 4:
        return 0.0
    total = 0.0
    for left, right in zip(ring, ring[1:]):
        total += float(left[0]) * float(right[1]) - float(right[0]) * float(left[1])
    return abs(total) / 2.0


def _geojson_from_rings(
    outer_rings: list[list[list[float]]],
    inner_rings: list[list[list[float]]],
) -> tuple[str | None, int]:
    if not outer_rings:
        return None, len(inner_rings)

    holes: list[list[list[list[float]]]] = [[] for _ in outer_rings]
    orphan = 0
    outer_areas = [_ring_area(ring) for ring in outer_rings]
    for inner in inner_rings:
        lon, lat = float(inner[0][0]), float(inner[0][1])
        containers = [
            idx for idx, outer in enumerate(outer_rings)
            if _point_in_ring(lon, lat, outer)
        ]
        if not containers:
            orphan += 1
            continue
        # Nested outers are unusual but legal enough to handle conservatively:
        # attach the hole to the smallest containing outer component.
        target = min(containers, key=lambda idx: outer_areas[idx])
        holes[target].append(inner)

    polygons = [
        [outer, *holes[idx]]
        for idx, outer in enumerate(outer_rings)
    ]
    if len(polygons) == 1:
        geom = {"type": "Polygon", "coordinates": polygons[0]}
    else:
        geom = {"type": "MultiPolygon", "coordinates": polygons}
    return json.dumps(geom, separators=(",", ":"), ensure_ascii=False), orphan


def recover_country_geometry(
    con: sqlite3.Connection,
    relation_id: int,
    *,
    overwrite: bool = False,
) -> CountryGeometryRecovery:
    relation_id = int(relation_id)
    row = con.execute(
        "SELECT pk FROM objects WHERE osm_type='relation' AND osm_id=?",
        (relation_id,),
    ).fetchone()
    if row is None:
        return CountryGeometryRecovery(relation_id, "missing_relation")

    object_pk = int(row["pk"])
    if not overwrite and get_area_geometry(con, object_pk) is not None:
        return CountryGeometryRecovery(relation_id, "existing")

    tags = get_tags(con, object_pk)
    if tags.get("boundary") != "administrative" or tags.get("admin_level") != "2":
        return CountryGeometryRecovery(relation_id, "not_country_boundary")

    outers, inners, missing, unusable = _load_segments(con, relation_id)
    outer_rings, open_outer = _assemble_closed_rings(outers)
    inner_rings, open_inner = _assemble_closed_rings(inners)
    geojson, orphan_inner = _geojson_from_rings(outer_rings, inner_rings)

    if geojson is None:
        return CountryGeometryRecovery(
            relation_id,
            "no_closed_outer",
            outer_rings=0,
            inner_rings=len(inner_rings),
            missing_way_members=missing,
            unusable_way_members=unusable,
            skipped_open_outer_components=open_outer,
            skipped_open_inner_components=open_inner,
            orphan_inner_rings=orphan_inner,
        )

    if not set_area_geometry(con, "relation", relation_id, geojson):
        return CountryGeometryRecovery(relation_id, "store_failed")

    return CountryGeometryRecovery(
        relation_id,
        "recovered",
        outer_rings=len(outer_rings),
        inner_rings=len(inner_rings) - orphan_inner,
        missing_way_members=missing,
        unusable_way_members=unusable,
        skipped_open_outer_components=open_outer,
        skipped_open_inner_components=open_inner,
        orphan_inner_rings=orphan_inner,
    )


def recover_selected_country_geometries(
    con: sqlite3.Connection,
    selected_iso2: Iterable[str],
    *,
    overwrite: bool = False,
) -> list[CountryGeometryRecovery]:
    selected = _normalize_iso2(selected_iso2)
    if not selected:
        return []

    area_clause = "" if overwrite else "AND area.object_pk IS NULL"
    candidates = list(
        con.execute(
            f"""SELECT o.pk,o.osm_id
               FROM objects o
               JOIN object_tags boundary
                 ON boundary.object_pk=o.pk
                AND boundary.key='boundary'
                AND boundary.value='administrative'
               JOIN object_tags level
                 ON level.object_pk=o.pk
                AND level.key='admin_level'
                AND level.value='2'
               LEFT JOIN area_geometries area ON area.object_pk=o.pk
               WHERE o.osm_type='relation' {area_clause}
               ORDER BY o.osm_id"""
        )
    )
    results: list[CountryGeometryRecovery] = []
    for row in candidates:
        tags = get_tags(con, int(row["pk"]))
        if not (_country_iso2(tags) & selected):
            continue
        results.append(
            recover_country_geometry(
                con,
                int(row["osm_id"]),
                overwrite=overwrite,
            )
        )
    return results


def _open_existing_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recover exact closed admin-level-2 boundary components from "
            "stored OSM relation/way topology without inventing missing edges"
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--country-code", action="append", default=[])
    parser.add_argument("--relation-id", type=int, action="append", default=[])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write recovered geometry to area_geometries",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing stored geometry for selected relations/countries",
    )
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required because this command modifies the database")
    if not args.country_code and not args.relation_id:
        parser.error("provide --country-code and/or --relation-id")

    con = _open_existing_db(args.db.resolve())
    try:
        con.execute("BEGIN IMMEDIATE")
        results: list[CountryGeometryRecovery] = []
        seen: set[int] = set()
        for relation_id in args.relation_id:
            relation_id = int(relation_id)
            if relation_id in seen:
                continue
            seen.add(relation_id)
            results.append(
                recover_country_geometry(
                    con,
                    relation_id,
                    overwrite=args.overwrite,
                )
            )
        for result in recover_selected_country_geometries(
            con,
            args.country_code,
            overwrite=args.overwrite,
        ):
            if result.relation_id in seen:
                continue
            seen.add(result.relation_id)
            results.append(result)

        check = con.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {check[0] if check else 'no result'}")
        con.commit()
        print(json.dumps([asdict(result) for result in results], indent=2))
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    main()
