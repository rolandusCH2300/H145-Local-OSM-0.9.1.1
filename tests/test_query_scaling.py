from __future__ import annotations

import sqlite3
import unittest

from local_osm import query
from local_osm.db import (
    OSMObject, connect, encode_way_geometry_v5, insert_object,
    insert_way_geometry_v5_rows,
)


class QueryScalingTests(unittest.TestCase):
    def test_large_recurse_preserves_every_node_without_global_membership_scan(self):
        con = connect(":memory:")
        self.addCleanup(con.close)
        geometry = [(10_000 + i, 60.451 + i * 1e-8, 22.264) for i in range(8005)]
        insert_object(con, OSMObject(
            "way", 101, 60.451, 22.264, 60.451, 60.452, 22.264, 22.264,
            {"highway": "primary"}, False,
        ))
        # A standalone tagged node overrides the embedded coordinate and must
        # retain every full tag. A way with the same public ID is not a node.
        insert_object(con, OSMObject(
            "node", 10_000, 60.452, 22.265, 60.452, 60.452, 22.265, 22.265,
            {"name": "Tagged descendant", "source": "preserve me"}, False,
        ))
        insert_object(con, OSMObject(
            "way", 10_001, 65.0, 25.0, 65.0, 65.0, 25.0, 25.0,
            {"building": "yes"}, False,
        ))
        node_ids, coords = encode_way_geometry_v5(geometry)
        insert_way_geometry_v5_rows(con, [(101, node_ids, coords)])
        con.commit()
        statements = []
        con.set_trace_callback(statements.append)
        actual = query.execute_overpass_subset(
            con, '[out:json];way(60.40,22.20,60.50,22.35)[highway=primary];(._;>;);out;',
        )
        con.set_trace_callback(None)
        expected = [{
            "type": "way", "id": 101, "center": {"lat": 60.451, "lon": 22.264},
            "nodes": [row[0] for row in geometry], "tags": {"highway": "primary"},
        }]
        # The packed geometry preserves coordinates to seven decimal places.
        for node_id, lat, lon in query.decode_way_geometry_v5(node_ids, coords):
            expected.append({"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": {}})
        expected[1] = {
            "type": "node", "id": 10_000, "lat": 60.452, "lon": 22.265,
            "tags": {"name": "Tagged descendant", "source": "preserve me"},
        }
        self.assertEqual(actual, expected)
        node_queries = [sql for sql in statements if "FROM objects WHERE osm_type='node'" in sql]
        self.assertTrue(node_queries)
        self.assertTrue(all("osm_id IN (" in sql for sql in node_queries), node_queries)

    def vocabulary_db(self, values, copies=1):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.executescript(
            "CREATE TABLE object_tags(object_pk INTEGER, key TEXT NOT NULL, value TEXT NOT NULL, "
            "PRIMARY KEY(object_pk,key)) WITHOUT ROWID;"
            "CREATE INDEX idx_tags_key_value ON object_tags(key,value,object_pk);"
        )
        con.executemany(
            "INSERT INTO object_tags VALUES(?, 'highway', ?)",
            ((i, value) for i, value in enumerate(value for value in values for _ in range(copies))),
        )
        return con

    def test_vocabulary_skips_duplicate_rows_and_preserves_regex_semantics(self):
        values = ["", "motorway", "motorway_link", "primary", "primary;service", "Überweg", "道", "a\x00b"]
        con = self.vocabulary_db(values, copies=3000)
        instructions = []
        con.set_progress_handler(lambda: instructions.append(1000) or 0, 1000)
        actual = query._tag_values_for_key(con, "highway")
        con.set_progress_handler(None, 0)
        expected = tuple(row[0] for row in con.execute(
            "SELECT DISTINCT value FROM object_tags WHERE key='highway' ORDER BY value",
        ))
        self.assertEqual(actual, expected)
        # A vocabulary lookup must not visit tens of thousands of duplicates.
        self.assertLess(sum(instructions), 10_000)
        self.assertEqual(query._regex_matching_values(con, "highway", "motorway"), ("motorway", "motorway_link"))
        self.assertEqual(query._regex_matching_values(con, "highway", "^motorway$"), ("motorway",))
        self.assertEqual(query._tag_values_for_key(con, "absent"), ())
        with self.assertRaises(query.UnsupportedQuery):
            query._regex_matching_values(con, "highway", "[")

    def test_large_vocabulary_fallback_never_truncates_values(self):
        values = [f"class-{i:05}" for i in range(5001)]
        con = self.vocabulary_db(values)
        self.assertEqual(query._tag_values_for_key(con, "highway"), tuple(values))
        self.assertEqual(query._regex_matching_values(con, "highway", "^class-05000$"), ("class-05000",))
        self.assertIsNone(query._regex_matching_values(con, "highway", ".*"))
        other = self.vocabulary_db(["different-snapshot"])
        self.assertEqual(query._tag_values_for_key(other, "highway"), ("different-snapshot",))

    def test_coverage_profile_preserves_legacy_wire_shape_and_accounts_once(self):
        profile = query.QueryProfile(candidate_sql_ms=2, coverage_prepare_ms=3, coverage_filter_ms=5, unprofiled_ms=7)
        self.assertEqual(profile.timed_ms(), 10)
        legacy = profile.as_dict()
        self.assertNotIn("coverage_prepare_ms", legacy)
        self.assertNotIn("coverage_filter_ms", legacy)
        self.assertEqual(legacy["timed_ms"], 2)
        self.assertEqual(legacy["unprofiled_ms"], 15)
        explicit = profile.as_dict(include_coverage=True)
        self.assertEqual(explicit["timed_ms"], 10)
        self.assertEqual(explicit["unprofiled_ms"], 7)
        self.assertEqual(explicit["coverage_prepare_ms"], 3)
        self.assertEqual(explicit["coverage_filter_ms"], 5)


if __name__ == "__main__":
    unittest.main()
