from __future__ import annotations

import base64
import json
import sqlite3
import urllib.request
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import geofabrik
from .query import AROUND_SELECTOR_RE, BBOX_SELECTOR_RE, IS_IN_PIVOT_RE

GEOFABRIK_GEOMETRY_INDEX_URL = "https://download.geofabrik.de/index-v1.json"
COVERAGE_METADATA_KEY = "coverage_guard_v1"
COVERAGE_VERSION = 1
_POINT_CACHE_SIZE = 32_768
_BOUNDARY_EPSILON = 1e-10


class CoverageMiss(RuntimeError):
    """Raised when an OSM query is anchored in known unloaded coverage."""

    def __init__(self, detail: str, diagnostic: dict[str, Any]):
        super().__init__(detail)
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Allowed-query diagnostics, currently used for mixed BBOX coverage."""

    diagnostic: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _PreparedRing:
    south: float
    north: float
    scale: float
    # Each latitude bucket holds original edges, in their original order.
    buckets: tuple[tuple[tuple[tuple[float, float], tuple[float, float]], ...], ...]


@dataclass(frozen=True, slots=True)
class _IndexedFeature:
    record: dict
    bounds: tuple[float, float, float, float]  # west, south, east, north
    polygons: tuple[tuple[_PreparedRing, ...], ...]


def fetch_geofabrik_geometry_index() -> dict:
    request = urllib.request.Request(
        GEOFABRIK_GEOMETRY_INDEX_URL,
        headers={"User-Agent": geofabrik.USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _feature_record(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    geometry = feature.get("geometry")
    region_id = str(props.get("id") or "").strip()
    if not region_id or not isinstance(geometry, dict):
        return None
    return {
        "id": region_id,
        "name": str(props.get("name") or region_id),
        "iso2": [str(code).upper() for code in (props.get("iso3166-1:alpha2") or [])],
        "geometry": geometry,
    }


def build_geofabrik_coverage_payload(index: dict, selected_queries: Sequence[str]) -> str:
    """Build compressed offline coverage metadata from Geofabrik's geometry index."""
    by_id: dict[str, dict] = {}
    countries: list[dict] = []
    for feature in index.get("features") or []:
        record = _feature_record(feature)
        if record is None:
            continue
        by_id[record["id"]] = record
        if record["iso2"]:
            countries.append(record)

    selected: list[dict] = []
    seen: set[str] = set()
    for query in selected_queries:
        # Keep region-name/ID/ISO matching exactly aligned with the existing
        # Geofabrik downloader instead of introducing a second resolver.
        resolved = geofabrik.resolve_region(index, str(query))
        region_id = str(resolved["id"])
        if region_id in seen:
            continue
        seen.add(region_id)
        record = by_id.get(region_id)
        if record is None:
            raise ValueError(
                f"Geofabrik geometry index has no polygon for selected region {region_id!r}"
            )
        selected.append(record)

    if not selected:
        raise ValueError("coverage metadata requires at least one selected Geofabrik region")

    raw = json.dumps(
        {
            "version": COVERAGE_VERSION,
            "source": "geofabrik-index-v1",
            "selected": selected,
            "countries": countries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")


def prepare_geofabrik_coverage(selected_queries: Sequence[str]) -> str:
    print(
        "[coverage] Fetching Geofabrik coverage boundaries before downloading OSM data "
        "(network timeout: 30 seconds per blocking operation)...",
        flush=True,
    )
    index = fetch_geofabrik_geometry_index()
    encoded = build_geofabrik_coverage_payload(index, selected_queries)
    decoded = _decode_payload(encoded)
    print(
        "[coverage] Geofabrik guard prepared: "
        f"selected={','.join(item['id'] for item in decoded['selected'])} "
        f"countries={len(decoded['countries'])} [OK]"
    )
    return encoded


def install_coverage_metadata(db_path: Path, encoded_payload: str) -> None:
    db_path = Path(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (COVERAGE_METADATA_KEY, encoded_payload),
        )
        con.execute(
            "INSERT INTO metadata(key,value) VALUES('coverage_guard_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(COVERAGE_VERSION),),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    print(f"[coverage] offline coverage guard metadata installed in {db_path}")


def _decode_payload(encoded: str) -> dict:
    try:
        raw = zlib.decompress(base64.b64decode(encoded.encode("ascii"), validate=True))
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid coverage guard metadata") from exc
    if not isinstance(value, dict) or int(value.get("version") or 0) != COVERAGE_VERSION:
        raise ValueError("unsupported coverage guard metadata version")
    return value


def _geometry_polygons(geometry: dict) -> list:
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    kind = str(geometry.get("type") or "")
    if kind == "Polygon":
        return [coordinates]
    if kind == "MultiPolygon":
        return coordinates
    return []


def _geometry_bounds(geometry: dict) -> tuple[float, float, float, float]:
    west = 180.0
    south = 90.0
    east = -180.0
    north = -90.0
    found = False
    for polygon in _geometry_polygons(geometry):
        for ring in polygon:
            for point in ring:
                if len(point) < 2:
                    continue
                lon, lat = float(point[0]), float(point[1])
                west = min(west, lon)
                south = min(south, lat)
                east = max(east, lon)
                north = max(north, lat)
                found = True
    return (west, south, east, north) if found else (0.0, 0.0, 0.0, 0.0)


def _index_feature(record: dict) -> _IndexedFeature:
    geometry = record.get("geometry")
    bounds = (
        _geometry_bounds(geometry)
        if isinstance(geometry, dict)
        else (0.0, 0.0, 0.0, 0.0)
    )
    polygons = tuple(
        tuple(_prepare_ring(ring) for ring in polygon)
        for polygon in _geometry_polygons(geometry)
    ) if isinstance(geometry, dict) else ()
    return _IndexedFeature(record=record, bounds=bounds, polygons=polygons)


def _latitude_bucket(lat: float, south: float, scale: float, count: int) -> int:
    return min(count - 1, max(0, int((lat - south) * scale)))


def _prepare_ring(ring: Sequence[Sequence[float]]) -> _PreparedRing:
    """Index edges by latitude without simplifying geometry or rounding points."""
    if len(ring) < 3:
        return _PreparedRing(0.0, 0.0, 0.0, ((),))
    points = tuple((float(point[0]), float(point[1])) for point in ring)
    south = min(point[1] for point in points) - _BOUNDARY_EPSILON
    north = max(point[1] for point in points) + _BOUNDARY_EPSILON
    count = max(1, min(256, len(points) // 8))
    scale = count / (north - south) if north > south else 0.0
    buckets: list[list] = [[] for _ in range(count)]
    previous = points[-1]
    for current in points:
        # Include the full tolerance interval used by _point_on_segment, as
        # well as every latitude that can cross the ray-casting edge.
        lower = min(previous[1], current[1]) - _BOUNDARY_EPSILON
        upper = max(previous[1], current[1]) + _BOUNDARY_EPSILON
        first = _latitude_bucket(lower, south, scale, count)
        last = _latitude_bucket(upper, south, scale, count)
        edge = (previous, current)
        for index in range(first, last + 1):
            buckets[index].append(edge)
        previous = current
    return _PreparedRing(south, north, scale, tuple(tuple(bucket) for bucket in buckets))


def _point_in_prepared_ring(lon: float, lat: float, ring: _PreparedRing) -> bool:
    if not ring.south <= lat <= ring.north:
        return False
    edges = ring.buckets[_latitude_bucket(lat, ring.south, ring.scale, len(ring.buckets))]
    inside = False
    for previous, current in edges:
        # Keep the original tolerance and floating-point operation order.
        if _point_on_segment(lon, lat, previous, current):
            return True
        x1, y1 = previous
        x2, y2 = current
        if (y1 > lat) != (y2 > lat):
            x_cross = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < x_cross:
                inside = not inside
    return inside


def _bounds_contain_point(bounds, lon: float, lat: float) -> bool:
    west, south, east, north = bounds
    return west <= lon <= east and south <= lat <= north


def _bounds_intersect_bbox(bounds, south: float, west: float, north: float, east: float) -> bool:
    bw, bs, be, bn = bounds
    return not (be < west or east < bw or bn < south or north < bs)


def _bounds_contain_bbox(bounds, south: float, west: float, north: float, east: float) -> bool:
    bw, bs, be, bn = bounds
    return bw <= west <= east <= be and bs <= south <= north <= bn


def _point_on_segment(
    lon: float,
    lat: float,
    a: Sequence[float],
    b: Sequence[float],
    *,
    epsilon: float = _BOUNDARY_EPSILON,
) -> bool:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cross = (lon - ax) * (by - ay) - (lat - ay) * (bx - ax)
    if abs(cross) > epsilon:
        return False
    return (
        min(ax, bx) - epsilon <= lon <= max(ax, bx) + epsilon
        and min(ay, by) - epsilon <= lat <= max(ay, by) + epsilon
    )


def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    if len(ring) < 3:
        return False
    inside = False
    previous = ring[-1]
    for current in ring:
        if _point_on_segment(lon, lat, previous, current):
            return True
        x1, y1 = float(previous[0]), float(previous[1])
        x2, y2 = float(current[0]), float(current[1])
        if (y1 > lat) != (y2 > lat):
            x_cross = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < x_cross:
                inside = not inside
        previous = current
    return inside


def _point_in_polygon(lon: float, lat: float, polygon: Sequence) -> bool:
    if not polygon or not _point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in polygon[1:])


def _point_in_geometry(lon: float, lat: float, geometry: dict) -> bool:
    return any(
        _point_in_polygon(lon, lat, polygon)
        for polygon in _geometry_polygons(geometry)
    )


def _point_in_feature(lon: float, lat: float, feature: _IndexedFeature) -> bool:
    if not _bounds_contain_point(feature.bounds, lon, lat):
        return False
    return any(
        polygon and _point_in_prepared_ring(lon, lat, polygon[0])
        and not any(_point_in_prepared_ring(lon, lat, hole) for hole in polygon[1:])
        for polygon in feature.polygons
    )


def _cached_point_lookup(features: tuple[_IndexedFeature, ...]):
    # Each immutable coverage snapshot owns its bounded cache. No coordinate
    # quantization, and no shared entries across databases or replacement guards.
    @lru_cache(maxsize=_POINT_CACHE_SIZE)
    def lookup(lat: float, lon: float) -> _IndexedFeature | None:
        return next((item for item in features if _point_in_feature(lon, lat, item)), None)

    return lookup


def _bbox_points(
    south: float,
    west: float,
    north: float,
    east: float,
) -> tuple[tuple[float, float], ...]:
    mid_lat = (south + north) / 2.0
    mid_lon = (west + east) / 2.0
    return (
        (west, south),
        (east, south),
        (east, north),
        (west, north),
        (mid_lon, south),
        (mid_lon, north),
        (west, mid_lat),
        (east, mid_lat),
        (mid_lon, mid_lat),
    )


def _orientation(a, b, c) -> float:
    return (float(b[0]) - float(a[0])) * (float(c[1]) - float(a[1])) - (
        float(b[1]) - float(a[1])
    ) * (float(c[0]) - float(a[0]))


def _segments_intersect(a, b, c, d, *, epsilon: float = 1e-10) -> bool:
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    if (
        ((o1 > epsilon and o2 < -epsilon) or (o1 < -epsilon and o2 > epsilon))
        and ((o3 > epsilon and o4 < -epsilon) or (o3 < -epsilon and o4 > epsilon))
    ):
        return True
    return (
        (abs(o1) <= epsilon and _point_on_segment(float(c[0]), float(c[1]), a, b))
        or (abs(o2) <= epsilon and _point_on_segment(float(d[0]), float(d[1]), a, b))
        or (abs(o3) <= epsilon and _point_on_segment(float(a[0]), float(a[1]), c, d))
        or (abs(o4) <= epsilon and _point_on_segment(float(b[0]), float(b[1]), c, d))
    )


def _point_in_bbox(lon, lat, south, west, north, east) -> bool:
    return west <= lon <= east and south <= lat <= north


def _ring_intersects_bbox(ring, south, west, north, east) -> bool:
    if not ring:
        return False
    edges = (
        ((west, south), (east, south)),
        ((east, south), (east, north)),
        ((east, north), (west, north)),
        ((west, north), (west, south)),
    )
    previous = ring[-1]
    for current in ring:
        if _point_in_bbox(
            float(current[0]), float(current[1]), south, west, north, east
        ):
            return True
        if any(_segments_intersect(previous, current, a, b) for a, b in edges):
            return True
        previous = current
    return False


def _geometry_intersects_bbox(geometry: dict, south, west, north, east) -> bool:
    if any(
        _point_in_geometry(lon, lat, geometry)
        for lon, lat in _bbox_points(south, west, north, east)
    ):
        return True
    return any(
        _ring_intersects_bbox(ring, south, west, north, east)
        for polygon in _geometry_polygons(geometry)
        for ring in polygon
    )


def _geometry_contains_bbox(geometry: dict, south, west, north, east) -> bool:
    # Nine probes avoid the common concave/coastal false positive from checking
    # only four corners. Any hole touching the rectangle makes containment fail.
    if not all(
        _point_in_geometry(lon, lat, geometry)
        for lon, lat in _bbox_points(south, west, north, east)
    ):
        return False
    return not any(
        ring_index > 0 and _ring_intersects_bbox(ring, south, west, north, east)
        for polygon in _geometry_polygons(geometry)
        for ring_index, ring in enumerate(polygon)
    )


def _feature_intersects_bbox(
    feature: _IndexedFeature,
    south,
    west,
    north,
    east,
) -> bool:
    if not _bounds_intersect_bbox(feature.bounds, south, west, north, east):
        return False
    geometry = feature.record.get("geometry")
    return isinstance(geometry, dict) and _geometry_intersects_bbox(
        geometry, south, west, north, east
    )


def _feature_contains_bbox(
    feature: _IndexedFeature,
    south,
    west,
    north,
    east,
) -> bool:
    if not _bounds_contain_bbox(feature.bounds, south, west, north, east):
        return False
    geometry = feature.record.get("geometry")
    return isinstance(geometry, dict) and _geometry_contains_bbox(
        geometry, south, west, north, east
    )


def _feature_is_selected_country(
    feature: _IndexedFeature,
    selected: Sequence[_IndexedFeature],
) -> bool:
    feature_id = str(feature.record.get("id") or "")
    return any(
        str(item.record.get("id") or "") == feature_id
        for item in selected
    )


class CoverageGuard:
    def __init__(
        self,
        selected: Iterable[dict],
        countries: Iterable[dict],
        *,
        enabled=True,
    ):
        self.selected = tuple(_index_feature(item) for item in selected)
        self.countries = tuple(_index_feature(item) for item in countries)
        self.enabled = bool(enabled and self.selected and self.countries)
        self._selected_point_lookup = _cached_point_lookup(self.selected)
        self._country_point_lookup = _cached_point_lookup(self.countries)

    @classmethod
    def from_connection(cls, con: sqlite3.Connection) -> "CoverageGuard":
        try:
            row = con.execute(
                "SELECT value FROM metadata WHERE key=?",
                (COVERAGE_METADATA_KEY,),
            ).fetchone()
        except sqlite3.Error:
            return cls((), (), enabled=False)
        if row is None:
            return cls((), (), enabled=False)
        value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            payload = _decode_payload(str(value))
        except ValueError:
            return cls((), (), enabled=False)
        return cls(payload.get("selected") or (), payload.get("countries") or ())

    @property
    def selected_ids(self) -> list[str]:
        return [str(item.record.get("id") or "") for item in self.selected]

    def _selected_for_point(self, lat, lon) -> _IndexedFeature | None:
        return self._selected_point_lookup(lat, lon)

    def _country_for_point(self, lat, lon) -> _IndexedFeature | None:
        return self._country_point_lookup(lat, lon)

    def _check_point(self, kind: str, lat: float, lon: float) -> None:
        if self._selected_for_point(lat, lon) is not None:
            return
        country = self._country_for_point(lat, lon)
        if country is None:
            # No known country polygon: preserve offshore/no-country mission behavior.
            return
        record = country.record
        diagnostic = {
            "classification": "outside_loaded_coverage",
            "action": "rejected",
            "selector": kind,
            "location": {"lat": lat, "lon": lon},
            "unloaded_region": {
                "id": record.get("id"),
                "name": record.get("name"),
                "iso2": record.get("iso2") or [],
            },
            "loaded_regions": self.selected_ids,
        }
        raise CoverageMiss(
            "Requested OSM location is outside installed Geofabrik coverage.",
            diagnostic,
        )

    def _check_bbox(self, south, west, north, east) -> dict | None:
        if south > north or west > east:
            return None

        selected_hits = [
            item
            for item in self.selected
            if _feature_intersects_bbox(item, south, west, north, east)
        ]
        unloaded_hits = [
            item
            for item in self.countries
            if _feature_intersects_bbox(item, south, west, north, east)
            and not _feature_is_selected_country(item, self.selected)
        ]

        # A: Entire BBOX lies inside installed extraction coverage.
        if any(
            _feature_contains_bbox(item, south, west, north, east)
            for item in self.selected
        ):
            return None

        # Allow loaded/unloaded overlap and report it in HTTP _local metadata
        # and the API log.
        if selected_hits and unloaded_hits:
            return {
                "classification": "bbox_loaded_unloaded_overlap",
                "action": "allowed",
                "bbox": [south, west, north, east],
                "loaded_regions": [
                    str(item.record.get("id") or "") for item in selected_hits
                ],
                "unloaded_regions": [
                    {
                        "id": item.record.get("id"),
                        "name": item.record.get("name"),
                        "iso2": item.record.get("iso2") or [],
                    }
                    for item in unloaded_hits
                ],
            }

        # B: No installed extraction intersects the box and one known country
        # fully contains it: fail before R&E treats an empty result as retryable.
        if not selected_hits:
            for country in self.countries:
                if not _feature_contains_bbox(
                    country, south, west, north, east
                ):
                    continue
                record = country.record
                diagnostic = {
                    "classification": "bbox_outside_loaded_coverage",
                    "action": "rejected",
                    "bbox": [south, west, north, east],
                    "unloaded_region": {
                        "id": record.get("id"),
                        "name": record.get("name"),
                        "iso2": record.get("iso2") or [],
                    },
                    "loaded_regions": self.selected_ids,
                }
                raise CoverageMiss(
                    "Requested OSM bounding box is outside installed Geofabrik coverage.",
                    diagnostic,
                )
        return None

    def check_query(self, query: str) -> CoverageResult:
        if not self.enabled:
            return CoverageResult()

        for match in IS_IN_PIVOT_RE.finditer(query):
            self._check_point(
                "is_in",
                float(match.group("lat")),
                float(match.group("lon")),
            )

        # Anchor-only by design: increasing radius cannot turn an unsupported
        # mission location into a valid one merely by reaching installed data.
        for match in AROUND_SELECTOR_RE.finditer(query):
            self._check_point(
                "around",
                float(match.group("lat")),
                float(match.group("lon")),
            )

        diagnostics: list[dict] = []
        for match in BBOX_SELECTOR_RE.finditer(query):
            debug = self._check_bbox(
                float(match.group("s")),
                float(match.group("w")),
                float(match.group("n")),
                float(match.group("e")),
            )
            if debug is not None:
                diagnostics.append(debug)

        if not diagnostics:
            return CoverageResult()
        if len(diagnostics) == 1:
            return CoverageResult(diagnostic=diagnostics[0])
        return CoverageResult(
            diagnostic={
                "classification": "multiple_bbox_coverage_diagnostics",
                "action": "allowed",
                "items": diagnostics,
            }
        )
