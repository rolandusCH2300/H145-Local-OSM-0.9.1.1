from __future__ import annotations

import math
import random
import unittest
from unittest.mock import patch

from local_osm import coverage


def polygon(rings):
    return {"type": "Polygon", "coordinates": rings}


def feature(geometry, name="region"):
    return {"id": name, "name": name, "iso2": ["XX"], "geometry": geometry}


class CoverageScalingTests(unittest.TestCase):
    def test_prepared_features_match_original_geometry_at_borders_holes_and_concavities(self):
        concave = [[0, 0], [4, 0], [4, 4], [3, 4], [3, 1], [1, 1], [1, 4], [0, 4], [0, 0]]
        hole = [[.25, .25], [.75, .25], [.75, .75], [.25, .75], [.25, .25]]
        geometries = [
            polygon([concave, hole]),
            polygon([list(reversed(concave)), list(reversed(hole))]),
            {"type": "MultiPolygon", "coordinates": [[concave, hole], [[[5, 0], [6, 0], [6, 1], [5, 1], [5, 0]]]]},
            polygon([[[179, -1], [-179, -1], [-179, 1], [179, 1], [179, -1]]]),
            polygon([[[0, 0], [1, 0], [1, 0], [2, 0], [0, 0]]]),
            polygon([[], [[0, 0], [1, 1]]]),
        ]
        rng = random.Random(927)
        for geometry in geometries:
            indexed = coverage._index_feature(feature(geometry))
            points = [(rng.uniform(-1, 7), rng.uniform(-1, 5)) for _ in range(400)]
            for rings in coverage._geometry_polygons(geometry):
                for ring in rings:
                    if not ring:
                        continue
                    for a, b in zip(ring[-1:] + ring[:-1], ring):
                        for lon, lat in (a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)):
                            points.extend((lon + dx, lat + dy) for dx, dy in (
                                (0, 0), (0, 1e-11), (0, -1e-11), (0, 1e-9), (0, -1e-9), (1e-11, 0), (-1e-11, 0),
                            ))
            for lon, lat in points:
                expected = coverage._bounds_contain_point(indexed.bounds, lon, lat) and coverage._point_in_geometry(lon, lat, geometry)
                self.assertEqual(coverage._point_in_feature(lon, lat, indexed), expected, (geometry, lon, lat))

    def test_bucket_boundaries_and_dense_ring_match_original_ray_casting(self):
        ring = [[2 * math.cos(i * math.tau / 512), math.sin(i * math.tau / 512)] for i in range(512)]
        ring.append(ring[0])
        prepared = coverage._prepare_ring(ring)
        points = []
        for i in range(len(prepared.buckets) + 1):
            lat = prepared.south + i / prepared.scale
            for y in (lat, math.nextafter(lat, -math.inf), math.nextafter(lat, math.inf)):
                points.extend((lon, y) for lon in (-2, -1.1, 0, .9, 2))
        for lon, lat in ring:
            points.extend(((lon, lat), (lon, lat + 1e-11), (lon, lat - 1e-11)))
        for lon, lat in points:
            self.assertEqual(
                coverage._point_in_prepared_ring(lon, lat, prepared),
                coverage._point_in_ring(lon, lat, ring),
                (lon, lat),
            )
        with patch.object(coverage, "_point_on_segment", wraps=coverage._point_on_segment) as edge_checks:
            self.assertTrue(coverage._point_in_prepared_ring(0, .12345, prepared))
            self.assertLess(edge_checks.call_count, len(ring) // 4)

    def test_exact_point_cache_is_bounded_and_isolated_per_snapshot(self):
        record = feature(polygon([[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]))
        with patch.object(coverage, "_POINT_CACHE_SIZE", 4):
            guard = coverage.CoverageGuard([record], [record])
            other = coverage.CoverageGuard([], [record])
        with patch.object(coverage, "_point_in_feature", wraps=coverage._point_in_feature) as checks:
            self.assertIsNotNone(guard._selected_for_point(.5, 1 - 1e-9))
            self.assertIsNone(guard._selected_for_point(.5, 1 + 1e-9))
            self.assertIsNotNone(guard._selected_for_point(.5, 1 - 1e-9))
            self.assertIsNone(guard._selected_for_point(.5, 1 + 1e-9))
            self.assertEqual(checks.call_count, 2)
        self.assertIsNone(other._selected_for_point(.5, .5))
        self.assertIsNotNone(other._country_for_point(.5, .5))
        for i in range(20):
            guard._selected_for_point(.5, i / 100)
            guard._country_for_point(.5, i / 100)
        self.assertEqual(guard._selected_point_lookup.cache_info().currsize, 4)
        self.assertEqual(guard._country_point_lookup.cache_info().currsize, 4)


if __name__ == "__main__":
    unittest.main()
