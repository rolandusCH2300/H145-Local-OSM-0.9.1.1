from __future__ import annotations

import sqlite3
import threading
import time
from functools import lru_cache
from typing import Any


_TLS = threading.local()
_INSTALLED = False


def _record_summary(feature) -> dict[str, Any]:
    record = feature.record
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "iso2": record.get("iso2") or [],
    }


def _combine_diagnostics(items: list[dict]) -> dict | None:
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return {
        "classification": "multiple_coverage_diagnostics",
        "action": "allowed_with_root_filter",
        "items": items,
    }


def install_coverage_runtime() -> None:
    """Install runtime coverage filtering for local Overpass queries.
    Discovery roots are limited to installed Geofabrik coverage. Country
    resolution remains strict for known unloaded land, while auxiliary
    spatial searches may expand into installed coverage.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from . import coverage as coverage_mod
    from . import query as query_mod

    original_check_query = coverage_mod.CoverageGuard.check_query
    original_candidate_rows = query_mod._candidate_rows
    original_road_intersections = query_mod._road_intersections
    original_road_intersection_elements = query_mod._road_intersection_elements
    original_execute = query_mod.execute_overpass_subset

    @lru_cache(maxsize=16)
    def guard_for_connection(con: sqlite3.Connection):
        # Cache the decoded coverage catalog per persistent reader connection.
        return coverage_mod.CoverageGuard.from_connection(con)

    def selected_contains(guard, lat: float, lon: float) -> bool:
        if not guard.enabled:
            return True
        return guard._selected_for_point(float(lat), float(lon)) is not None

    def runtime_check_query(self, query: str):
        if not self.enabled:
            return coverage_mod.CoverageResult()

        # Country resolution remains strict for known unloaded land.
        for match in query_mod.IS_IN_PIVOT_RE.finditer(query):
            self._check_point(
                "is_in",
                float(match.group("lat")),
                float(match.group("lon")),
            )

        diagnostics: list[dict] = []

        # around() is a search origin; returned roots are filtered by coverage.
        seen_around: set[tuple[float, float]] = set()
        for match in query_mod.AROUND_SELECTOR_RE.finditer(query):
            lat = float(match.group("lat"))
            lon = float(match.group("lon"))
            key = (lat, lon)
            if key in seen_around:
                continue
            seen_around.add(key)
            if self._selected_for_point(lat, lon) is not None:
                continue
            country = self._country_for_point(lat, lon)
            if country is None:
                # Preserve offshore/no-country searches.
                continue
            diagnostics.append(
                {
                    "classification": "around_anchor_outside_loaded_coverage",
                    "action": "allowed_with_root_filter",
                    "selector": "around",
                    "location": {"lat": lat, "lon": lon},
                    "unloaded_region": _record_summary(country),
                    "loaded_regions": self.selected_ids,
                }
            )

        # Bounding-box results are filtered to installed coverage.
        for match in query_mod.BBOX_SELECTOR_RE.finditer(query):
            south = float(match.group("s"))
            west = float(match.group("w"))
            north = float(match.group("n"))
            east = float(match.group("e"))
            if south > north or west > east:
                continue

            if any(
                coverage_mod._feature_contains_bbox(item, south, west, north, east)
                for item in self.selected
            ):
                continue

            selected_hits = [
                item
                for item in self.selected
                if coverage_mod._feature_intersects_bbox(item, south, west, north, east)
            ]
            unloaded_hits = [
                item
                for item in self.countries
                if coverage_mod._feature_intersects_bbox(item, south, west, north, east)
                and not coverage_mod._feature_is_selected_country(item, self.selected)
            ]

            if selected_hits and unloaded_hits:
                diagnostics.append(
                    {
                        "classification": "bbox_loaded_unloaded_overlap",
                        "action": "allowed_with_root_filter",
                        "bbox": [south, west, north, east],
                        "loaded_regions": [
                            str(item.record.get("id") or "") for item in selected_hits
                        ],
                        "unloaded_regions": [_record_summary(item) for item in unloaded_hits],
                    }
                )
                continue

            if not selected_hits:
                containing = next(
                    (
                        country
                        for country in self.countries
                        if coverage_mod._feature_contains_bbox(
                            country, south, west, north, east
                        )
                    ),
                    None,
                )
                if containing is not None:
                    diagnostics.append(
                        {
                            "classification": "bbox_outside_loaded_coverage",
                            "action": "allowed_with_root_filter",
                            "bbox": [south, west, north, east],
                            "unloaded_region": _record_summary(containing),
                            "loaded_regions": self.selected_ids,
                        }
                    )

        return coverage_mod.CoverageResult(
            diagnostic=_combine_diagnostics(diagnostics)
        )

    def runtime_candidate_rows(
        con: sqlite3.Connection,
        sel,
        profile=None,
    ):
        rows = original_candidate_rows(con, sel, profile)
        guard = getattr(_TLS, "guard", None)
        if guard is None or not guard.enabled or getattr(_TLS, "road_intersection", False):
            return rows

        started = time.perf_counter()
        kept = []
        filtered = 0
        for row in rows:
            if selected_contains(
                guard,
                float(row["center_lat"]),
                float(row["center_lon"]),
            ):
                kept.append(row)
            else:
                filtered += 1
        if filtered:
            _TLS.filtered_roots = int(getattr(_TLS, "filtered_roots", 0)) + filtered
        if profile is not None:
            profile.coverage_filter_ms += (time.perf_counter() - started) * 1000.0
        return kept

    def runtime_road_intersection_elements(
        con: sqlite3.Connection,
        node_ids,
        coord_by_node,
        profile,
    ):
        elements = original_road_intersection_elements(
            con, node_ids, coord_by_node, profile
        )
        guard = getattr(_TLS, "guard", None)
        if guard is None or not guard.enabled:
            return elements

        started = time.perf_counter()
        kept = []
        filtered = 0
        for element in elements:
            lat = element.get("lat")
            lon = element.get("lon")
            if lat is None or lon is None or selected_contains(guard, float(lat), float(lon)):
                kept.append(element)
            else:
                filtered += 1
        if filtered:
            _TLS.filtered_roots = int(getattr(_TLS, "filtered_roots", 0)) + filtered
        if profile is not None:
            profile.coverage_filter_ms += (time.perf_counter() - started) * 1000.0
        return kept

    def runtime_road_intersections(con, selectors, profile=None):
        previous = getattr(_TLS, "road_intersection", False)
        _TLS.road_intersection = True
        try:
            # Filter road intersections by the result node, not the way midpoint.
            return original_road_intersections(con, selectors, profile)
        finally:
            _TLS.road_intersection = previous

    def runtime_execute_overpass_subset(
        con: sqlite3.Connection,
        query: str,
        profile=None,
    ):
        previous_guard = getattr(_TLS, "guard", None)
        previous_filtered = getattr(_TLS, "filtered_roots", 0)
        previous_road = getattr(_TLS, "road_intersection", False)
        started = time.perf_counter()
        _TLS.guard = guard_for_connection(con)
        if profile is not None:
            profile.coverage_prepare_ms += (time.perf_counter() - started) * 1000.0
        _TLS.filtered_roots = 0
        _TLS.road_intersection = False
        try:
            return original_execute(con, query, profile=profile)
        finally:
            _TLS.guard = previous_guard
            _TLS.filtered_roots = previous_filtered
            _TLS.road_intersection = previous_road

    coverage_mod.CoverageGuard.check_query = runtime_check_query
    query_mod._candidate_rows = runtime_candidate_rows
    query_mod._road_intersection_elements = runtime_road_intersection_elements
    query_mod._road_intersections = runtime_road_intersections
    query_mod.execute_overpass_subset = runtime_execute_overpass_subset
    query_mod._coverage_runtime_installed = True

    # Expose originals for tests and diagnostics.
    query_mod._coverage_runtime_original_execute = original_execute
    coverage_mod._coverage_runtime_original_check_query = original_check_query

    _INSTALLED = True
