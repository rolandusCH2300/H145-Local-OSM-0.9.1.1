import json
import tempfile
import unittest
from pathlib import Path

from local_osm.db import OSMObject, connect, get_tags
from local_osm.import_optimizations import insert_new_object_without_rtree
from local_osm.importer import ImportObjectBatch, _create_bulk_load_indexes, _drop_bulk_load_indexes
from local_osm.query import execute_overpass_subset
from local_osm.tier_c import (
    finalize_tier_c_tags,
    prepare_tier_c_schema,
    selector_contract_sql_violations,
    selector_tag_items,
    verify_tier_c_identity,
)


class TierCSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "tier-c.sqlite3"
        self.con = connect(self.db)
        self.con.execute("PRAGMA foreign_keys=OFF")
        prepare_tier_c_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_schema_uses_small_sequential_identity_without_composite_unique_index(self):
        object_sql = str(self.con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='objects'"
        ).fetchone()[0])
        self.assertNotIn("UNIQUE(osm_type, osm_id)", object_sql)
        self.assertIn("has_tags", object_sql)
        first = insert_new_object_without_rtree(self.con, OSMObject(
            "way", 9_000_000_000, 60, 24, 60, 60, 24, 24, {}, False,
        ))
        second = insert_new_object_without_rtree(self.con, OSMObject(
            "node", 12_000_000_000, 60, 24, 60, 60, 24, 24, {}, False,
        ))
        self.assertEqual((first, second), (1, 2))

    def test_selector_profile_drops_metadata_but_spool_preserves_full_tags(self):
        tags = {
            "highway": "service",
            "name": "Test road",
            "surface": "asphalt",
            "source": "survey",
        }
        pk = insert_new_object_without_rtree(self.con, OSMObject(
            "way", 100, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1, tags, False,
        ))
        self.con.commit()
        normalized = dict(self.con.execute(
            "SELECT key,value FROM object_tags WHERE object_pk=?", (pk,)
        ))
        self.assertEqual(normalized, {"highway": "service"})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM packed_object_tags").fetchone()[0], 0)
        rows, _payload, _elapsed = finalize_tier_c_tags(self.con)
        self.assertEqual(rows, 1)
        raw = self.con.execute(
            "SELECT tags_json FROM packed_object_tags WHERE object_pk=?", (pk,)
        ).fetchone()[0]
        self.assertEqual(json.loads(raw), tags)

    def test_untagged_objects_need_no_packed_row(self):
        insert_new_object_without_rtree(self.con, OSMObject(
            "way", 1, 60, 24, 60, 60, 24, 24, {}, False,
        ))
        insert_new_object_without_rtree(self.con, OSMObject(
            "way", 2, 60, 24, 60, 60, 24, 24, {"highway": "track"}, False,
        ))
        rows, _payload, _elapsed = finalize_tier_c_tags(self.con)
        self.assertEqual(rows, 1)
        self.assertEqual(self.con.execute("SELECT SUM(has_tags) FROM objects").fetchone()[0], 1)

    def test_exact_profile_values_only_are_normalized(self):
        tags = {
            "building": "garage",
            "natural": "cliff",
            "name": "Cliff garage",
        }
        self.assertEqual(dict(selector_tag_items(tags)), {"natural": "cliff"})

    def test_country_relations_keep_all_normalized_tags_for_legacy_country_paths(self):
        tags = {
            "type": "boundary",
            "boundary": "administrative",
            "admin_level": "2",
            "name": "Finland",
            "name:en": "Finland",
            "ISO3166-1": "FI",
            "ISO3166-1:alpha2": "FI",
            "wikidata": "Q33",
        }
        pk = insert_new_object_without_rtree(self.con, OSMObject(
            "relation", 500, 64.0, 26.0, 59.0, 70.0, 19.0, 32.0, tags, True,
        ))
        self.con.commit()
        self.assertEqual(get_tags(self.con, pk), tags)
        self.assertEqual(dict(selector_tag_items(tags)), tags)

    def test_identity_verifier_allows_cross_type_osm_id_but_rejects_duplicate_type(self):
        self.con.executemany(
            """INSERT INTO objects
               (osm_type,osm_id,center_lat,center_lon,min_lat,max_lat,min_lon,max_lon,area_eligible,has_tags)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [
                ("node", 123, 0, 0, 0, 0, 0, 0, 0, 0),
                ("way", 123, 0, 0, 0, 0, 0, 0, 0, 0),
            ],
        )
        self.con.execute("CREATE INDEX idx_objects_osm_id ON objects(osm_id)")
        verify_tier_c_identity(self.con)
        self.con.execute(
            """INSERT INTO objects
               (osm_type,osm_id,center_lat,center_lon,min_lat,max_lat,min_lon,max_lon,area_eligible,has_tags)
               VALUES('way',123,0,0,0,0,0,0,0,0)"""
        )
        with self.assertRaises(RuntimeError):
            verify_tier_c_identity(self.con)

    def test_selector_contract_has_no_extra_rows(self):
        insert_new_object_without_rtree(self.con, OSMObject(
            "way", 2, 60.0, 24.0, 60.0, 60.0, 24.0, 24.0,
            {"highway": "track", "name": "Forest track"}, False,
        ))
        self.assertEqual(selector_contract_sql_violations(self.con), 0)


class TierCBatchAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "tier-c-query.sqlite3"
        self.con = connect(self.db)
        self.con.execute("PRAGMA foreign_keys=OFF")
        prepare_tier_c_schema(self.con)
        _drop_bulk_load_indexes(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_batch_writer_uses_sequential_pk_inline_rtree_and_spooled_hydration(self):
        batch = ImportObjectBatch(self.con, object_limit=2, tag_limit=10, defer_rtree=True)
        pk = batch.add(OSMObject(
            "way", 700, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
            {"highway": "primary", "name": "Road 700", "surface": "asphalt"}, False,
        ))
        batch.flush()
        self.con.commit()
        self.assertEqual(pk, 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0], 1)
        self.assertEqual(
            dict(self.con.execute("SELECT key,value FROM object_tags WHERE object_pk=?", (pk,))),
            {"highway": "primary"},
        )
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM packed_object_tags").fetchone()[0], 0)
        finalize_tier_c_tags(self.con)
        self.assertEqual(
            json.loads(self.con.execute(
                "SELECT tags_json FROM packed_object_tags WHERE object_pk=?", (pk,)
            ).fetchone()[0]),
            {"highway": "primary", "name": "Road 700", "surface": "asphalt"},
        )

    def test_runtime_query_matches_selector_and_returns_full_nonselector_tags(self):
        batch = ImportObjectBatch(self.con, object_limit=10, tag_limit=100, defer_rtree=True)
        batch.add(OSMObject(
            "way", 701, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
            {"highway": "primary", "name": "Tier C road", "surface": "asphalt"}, False,
        ))
        batch.flush()
        self.con.commit()
        _create_bulk_load_indexes(self.con)
        finalize_tier_c_tags(self.con)
        self.con.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version','6') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        self.con.commit()

        result = execute_overpass_subset(
            self.con,
            "[out:json];way[highway=primary](59.0,23.0,61.0,25.0);out center;",
        )
        self.assertEqual(len(result), 1)
        element = result[0]
        self.assertEqual(element["type"], "way")
        self.assertEqual(element["id"], 701)
        self.assertEqual(element["tags"]["highway"], "primary")
        self.assertEqual(element["tags"]["name"], "Tier C road")
        self.assertEqual(element["tags"]["surface"], "asphalt")


if __name__ == "__main__":
    unittest.main()
