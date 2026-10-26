import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_osm.coverage import COVERAGE_METADATA_KEY, CoverageGuard, build_geofabrik_coverage_payload
from local_osm.db import OSMObject, connect
from local_osm.importer import ImportObjectBatch, _create_bulk_load_indexes, _drop_bulk_load_indexes
from local_osm.query import QueryProfile, _candidate_rows, execute_overpass_subset, parse_selectors
from local_osm.tier_c import finalize_tier_c_tags, prepare_tier_c_schema


class SelectorFilterRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "selector-filter.sqlite3"
        self.con = connect(self.db)
        self.con.execute("PRAGMA foreign_keys=OFF")
        prepare_tier_c_schema(self.con)
        _drop_bulk_load_indexes(self.con)

        batch = ImportObjectBatch(self.con, object_limit=100, tag_limit=1000, defer_rtree=True)

        # [amenity=school] is normalized; [name] is authoritative packed metadata.
        batch.add(OSMObject(
            "way", 100, 60.0000, 24.0000, 59.999, 60.001, 23.999, 24.001,
            {"amenity": "school", "name": "Named school"}, False,
        ))
        batch.add(OSMObject(
            "way", 101, 60.0010, 24.0010, 60.000, 60.002, 24.000, 24.002,
            {"amenity": "school"}, False,
        ))

        # Reproduces the R&E pitch selector pattern. leisure=pitch is indexed;
        # sport=football/surface/indoor are intentionally outside the selector profile.
        batch.add(OSMObject(
            "way", 200, 60.0020, 24.0020, 60.001, 60.003, 24.001, 24.003,
            {
                "leisure": "pitch",
                "sport": "soccer",
                "surface": "grass",
                "name": "Named grass pitch",
            },
            False,
        ))
        batch.add(OSMObject(
            "way", 201, 60.0030, 24.0030, 60.002, 60.004, 24.002, 24.004,
            {
                "leisure": "pitch",
                "sport": "soccer",
                "surface": "artificial_turf",
            },
            False,
        ))

        # Both natural=water and water=lake are normalized, while name is not.
        batch.add(OSMObject(
            "way", 300, 60.0040, 24.0040, 60.003, 60.005, 24.003, 24.005,
            {"natural": "water", "water": "lake", "name": "Named lake"}, False,
        ))
        batch.add(OSMObject(
            "way", 301, 60.0050, 24.0050, 60.004, 60.006, 24.004, 24.006,
            {"natural": "water", "water": "lake"}, False,
        ))

        # building=yes is a normalized exact value; name is packed-only.
        batch.add(OSMObject(
            "way", 400, 60.0060, 24.0060, 60.005, 60.007, 24.005, 24.007,
            {"building": "yes", "name": "Named building"}, False,
        ))
        batch.add(OSMObject(
            "node", 401, 60.0070, 24.0070, 60.007, 60.007, 24.007, 24.007,
            {"building": "yes", "name": "Named building node"}, False,
        ))

        batch.flush()
        self.con.commit()
        _create_bulk_load_indexes(self.con)
        finalize_tier_c_tags(self.con)
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _enable_coverage(self, rings):
        feature = {
            "type": "Feature",
            "properties": {
                "id": "test-region", "name": "Test region", "parent": "europe",
                "iso3166-1:alpha2": ["FI"],
                "urls": {"pbf": "https://example.invalid/test-region.osm.pbf"},
            },
            "geometry": {"type": "Polygon", "coordinates": rings},
        }
        payload = build_geofabrik_coverage_payload(
            {"type": "FeatureCollection", "features": [feature]}, ["test-region"],
        )
        self.con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
            (COVERAGE_METADATA_KEY, payload),
        )
        self.con.commit()
        # Coverage guards belong to one immutable reader snapshot.
        self.con.close()
        self.con = connect(self.db)

    def test_packed_predicate_precedes_coverage_for_unnamed_candidates(self):
        query = '[out:json];way(59.9,23.9,60.1,24.1)[amenity=school][name];out center;'
        expected = execute_overpass_subset(self.con, query)
        self.assertEqual([item["id"] for item in expected], [100])
        self._enable_coverage([
            [[23.9, 59.9], [24.1, 59.9], [24.1, 60.1], [23.9, 60.1], [23.9, 59.9]],
        ])
        checked = []
        original = CoverageGuard._selected_for_point

        def record_check(guard, lat, lon):
            checked.append((lat, lon))
            return original(guard, lat, lon)

        profile = QueryProfile()
        with patch.object(CoverageGuard, "_selected_for_point", new=record_check):
            actual = execute_overpass_subset(self.con, query, profile=profile)
        self.assertEqual(actual, expected)
        self.assertEqual(profile.candidate_rows, 2)
        self.assertEqual(checked, [(60.0, 24.0)])
        self.assertGreater(profile.coverage_filter_ms, 0)
        self.assertGreater(profile.candidate_filter_ms, 0)

    def test_full_tag_filters_preserve_holes_boundaries_and_unloaded_roots(self):
        filters = [
            '[amenity=school][name]',
            '[amenity=school][name~"^Named"]',
            '[amenity=school][name!="Named school"]',
            '[leisure=pitch][sport=soccer]',
            '[leisure=pitch][surface!=artificial_turf][indoor!=yes]',
            '[natural=water][water=lake][name]',
            '[building=yes][name]',
        ]
        queries = ['[out:json];nwr(59.9,23.9,60.1,24.1)' + item + ';out center;' for item in filters]
        # Preserve complete payloads for roots inside the polygon; school 100
        # and lake 300 lie on its boundary. Pitch 200 lies in the hole, and
        # buildings 400/401 lie beyond the selected region.
        loaded_ids = {100, 101, 201, 300}
        expected = [
            [item for item in execute_overpass_subset(self.con, query) if item["id"] in loaded_ids]
            for query in queries
        ]
        self._enable_coverage([
            [[24.0, 60.0], [24.004, 60.0], [24.004, 60.004], [24.0, 60.004], [24.0, 60.0]],
            [[24.0015, 60.0015], [24.0025, 60.0015], [24.0025, 60.0025],
             [24.0015, 60.0025], [24.0015, 60.0015]],
        ])
        for query, reference in zip(queries, expected):
            with self.subTest(query=query):
                self.assertEqual(execute_overpass_subset(self.con, query), reference)

    def test_secondary_exists_filter_uses_packed_tags_not_zero_row_seed(self):
        query = (
            '[out:json];(way(around:5000,60.0000,24.0000)'
            '[amenity="school"][name];); out center;'
        )
        profile = QueryProfile()
        result = execute_overpass_subset(self.con, query, profile=profile)
        self.assertEqual([item["id"] for item in result], [100])
        self.assertGreater(profile.candidate_rows, 0)
        self.assertEqual(profile.exists_tag_seeded_calls, 0)
        self.assertGreaterEqual(profile.packed_tags_calls, 1)

    def test_secondary_exact_and_negative_filters_use_full_tags(self):
        query = (
            '[out:json];(nwr(around:5000,60.0000,24.0000)'
            '[leisure=pitch][sport=soccer][surface!=artificial_turf][indoor!=yes];);'
            'out center;'
        )
        result = execute_overpass_subset(self.con, query)
        self.assertEqual([item["id"] for item in result], [200])

    def test_packed_name_filter_preserves_actual_mission_shapes(self):
        queries = [
            (
                '[out:json];(way(around:5000,60.0000,24.0000)'
                '[leisure="pitch"][name];); (._;>;); out;',
                {200},
            ),
            (
                '[out:json];(way(around:5000,60.0000,24.0000)'
                '[natural="water"][water="lake"][name];);(._;>;); out center;',
                {300},
            ),
            (
                '[out:json];(way(around:5000,60.0000,24.0000)'
                '[building="yes"][name]; node(around:5000,60.0000,24.0000)'
                '[building="yes"][name];); out center;',
                {400, 401},
            ),
        ]
        for query, expected_ids in queries:
            with self.subTest(query=query):
                result = execute_overpass_subset(self.con, query)
                root_ids = {item["id"] for item in result if item["id"] in expected_ids}
                self.assertEqual(root_ids, expected_ids)

    def test_selector_table_remains_compact(self):
        school_pk = int(self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='way' AND osm_id=100"
        ).fetchone()[0])
        pitch_pk = int(self.con.execute(
            "SELECT pk FROM objects WHERE osm_type='way' AND osm_id=200"
        ).fetchone()[0])
        self.assertEqual(
            dict(self.con.execute(
                "SELECT key,value FROM object_tags WHERE object_pk=?", (school_pk,)
            )),
            {"amenity": "school"},
        )
        self.assertEqual(
            dict(self.con.execute(
                "SELECT key,value FROM object_tags WHERE object_pk=?", (pitch_pk,)
            )),
            {"leisure": "pitch"},
        )


if __name__ == "__main__":
    unittest.main()
