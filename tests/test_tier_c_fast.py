import json
import tempfile
import unittest
from pathlib import Path

from local_osm.db import OSMObject, connect
from local_osm.importer import ImportObjectBatch, _create_bulk_load_indexes
from local_osm.tier_c import (
    finalize_tier_c_tags,
    prepare_tier_c_schema,
    verify_tier_c_identity,
)
from local_osm.tier_c_fast import PreparedTagPayload, _prepare_way_batch_fast


class FastTierCImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / "fast-v6.sqlite3")
        self.con.execute("PRAGMA foreign_keys=OFF")
        prepare_tier_c_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_small_sequential_pks_inline_rtree_and_spooled_full_tags(self):
        batch = ImportObjectBatch(self.con, object_limit=10, tag_limit=100, defer_rtree=True)
        first = batch.add(OSMObject(
            "way", 9_000_000_001,
            60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
            {"highway": "primary", "name": "Fast road", "surface": "asphalt"}, False,
        ))
        second = batch.add(OSMObject(
            "way", 9_000_000_002,
            61.0, 25.0, 60.9, 61.1, 24.9, 25.1,
            {}, False,
        ))
        batch.flush()
        self.con.commit()
        self.assertEqual((first, second), (1, 2))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0], 2)
        self.assertEqual(
            dict(self.con.execute("SELECT key,value FROM object_tags WHERE object_pk=1")),
            {"highway": "primary"},
        )
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM packed_object_tags").fetchone()[0], 0)
        rows, _payload, _elapsed = finalize_tier_c_tags(self.con)
        self.assertEqual(rows, 1)
        tags = json.loads(self.con.execute(
            "SELECT tags_json FROM packed_object_tags WHERE object_pk=1"
        ).fetchone()[0])
        self.assertEqual(tags["name"], "Fast road")
        self.assertEqual(tags["surface"], "asphalt")

    def test_worker_prepares_selector_and_full_tag_payload(self):
        records = [
            (
                100,
                ((1, 60.0, 24.0), (2, 60.1, 24.1)),
                (("highway", "primary"), ("name", "Road"), ("surface", "asphalt")),
                False,
                True,
            ),
            (
                101,
                ((3, 60.2, 24.2), (4, 60.3, 24.3)),
                (
                    ("boundary", "administrative"),
                    ("admin_level", "2"),
                    ("name", "Country boundary segment"),
                ),
                False,
                False,
            ),
        ]
        prepared, _coords, missing, retained, deps, refs, _worker_s = _prepare_way_batch_fast(records)
        self.assertEqual((missing, retained, deps, refs), (0, 1, 1, 4))

        payload = prepared[0][8]
        self.assertIsInstance(payload, PreparedTagPayload)
        self.assertEqual(dict(payload), {"highway": "primary", "name": "Road", "surface": "asphalt"})
        self.assertEqual(dict(payload.selector_items), {"highway": "primary"})
        self.assertIn(b'"name":"Road"', payload.packed_blob)

        country_payload = prepared[1][8]
        self.assertEqual(
            dict(country_payload.selector_items),
            {
                "boundary": "administrative",
                "admin_level": "2",
                "name": "Country boundary segment",
            },
        )

    def test_identity_index_is_deferred_and_duplicate_verification_is_hard_gate(self):
        batch = ImportObjectBatch(self.con)
        batch.add(OSMObject("node", 42, 60, 24, 60, 60, 24, 24, {"amenity": "hospital"}, False))
        batch.add(OSMObject("way", 42, 60, 24, 60, 60, 24, 24, {"highway": "primary"}, False))
        batch.flush()
        self.con.commit()
        self.assertIsNone(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_objects_osm_id'"
        ).fetchone())
        _create_bulk_load_indexes(self.con)
        self.assertIsNotNone(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_objects_osm_id'"
        ).fetchone())
        verify_tier_c_identity(self.con)
        self.con.execute(
            """INSERT INTO objects
               (osm_type,osm_id,center_lat,center_lon,min_lat,max_lat,min_lon,max_lon,area_eligible,has_tags)
               VALUES('way',42,0,0,0,0,0,0,0,0)"""
        )
        with self.assertRaises(RuntimeError):
            verify_tier_c_identity(self.con)


if __name__ == "__main__":
    unittest.main()
