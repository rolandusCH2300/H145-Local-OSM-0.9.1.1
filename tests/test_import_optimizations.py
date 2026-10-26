import tempfile
import unittest
from pathlib import Path

from local_osm.db import OSMObject, connect
from local_osm.import_optimizations import (
    PackedRelationMembers,
    PendingRelation,
    compact_int_ids,
    insert_new_object_without_rtree,
    location_cache_used_memory,
    rebuild_object_rtree,
    verify_foreign_keys,
)
from local_osm.importer import ImportObjectBatch, TOPOLOGY_WORKER_BATCH_SIZE
from local_osm.tier_c import prepare_tier_c_schema


class PackedRelationTests(unittest.TestCase):
    def test_member_roundtrip_preserves_type_id_role_and_order(self):
        members = [
            ("node", 9_876_543_210, ""),
            ("way", 123_456_789, "outer"),
            ("relation", 42, "custom role"),
            ("way", 7, "inner"),
        ]
        packed = PackedRelationMembers.pack(members)
        self.assertEqual(list(packed), members)
        self.assertEqual(len(packed), len(members))
        self.assertEqual(packed.member_ids.itemsize, 8)
        self.assertEqual(len(packed.type_codes), len(members))

    def test_pending_relation_packs_members_automatically(self):
        members = [("way", 10, "outer"), ("node", 20, "via")]
        relation = PendingRelation(100, {"type": "multipolygon"}, members)
        self.assertIsInstance(relation.members, PackedRelationMembers)
        self.assertEqual(list(relation.members), members)

    def test_compact_sparse_ids_remain_exact(self):
        ids = {1, 999_999_999, 12_345_678_901}
        compact = compact_int_ids(ids)
        self.assertEqual(set(compact), ids)
        self.assertEqual(compact.itemsize, 8)


class RTreeStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / "rtree.sqlite3")
        self.con.execute("PRAGMA foreign_keys=OFF")
        prepare_tier_c_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_hot_batch_overrides_defer_flag_and_keeps_rtree_inline(self):
        batch = ImportObjectBatch(
            self.con,
            object_limit=2,
            tag_limit=10,
            defer_rtree=True,
        )
        first = batch.add(OSMObject(
            "way", 100, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
            {"highway": "primary"}, False,
        ))
        batch.add(OSMObject(
            "way", 101, 61.0, 25.0, 60.9, 61.1, 24.9, 25.1,
            {"natural": "cliff"}, True,
        ))
        batch.flush()
        self.con.commit()

        self.assertEqual(first, 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 2)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0], 2)
        row = self.con.execute(
            "SELECT min_lon,max_lon,min_lat,max_lat FROM object_rtree WHERE pk=?",
            (first,),
        ).fetchone()
        self.assertAlmostEqual(float(row[0]), 23.9, places=4)
        self.assertAlmostEqual(float(row[1]), 24.1, places=4)
        self.assertAlmostEqual(float(row[2]), 59.9, places=4)
        self.assertAlmostEqual(float(row[3]), 60.1, places=4)

    def test_single_object_helper_still_supports_deferred_rtree_low_volume_path(self):
        pk = insert_new_object_without_rtree(self.con, OSMObject(
            "node", 55, 60.5, 24.5, 60.5, 60.5, 24.5, 24.5,
            {"amenity": "hospital"}, False,
        ))
        self.con.commit()
        self.assertEqual(pk, 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0], 0)
        self.assertEqual(
            tuple(self.con.execute("SELECT lat,lon FROM node_coords WHERE node_id=55").fetchone()),
            (60.5, 24.5),
        )
        rows, _elapsed = rebuild_object_rtree(self.con)
        self.assertEqual(rows, 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM object_rtree").fetchone()[0], 1)


class VerificationAndInstrumentationTests(unittest.TestCase):
    def test_deferred_foreign_key_check_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "fk.sqlite3")
            try:
                con.commit()
                con.execute("PRAGMA foreign_keys=OFF")
                con.execute(
                    "INSERT INTO object_tags(object_pk,key,value) VALUES(?,?,?)",
                    (999, "highway", "service"),
                )
                with self.assertRaises(RuntimeError):
                    verify_foreign_keys(con)
            finally:
                con.close()

    def test_location_cache_memory_is_reported_without_mutation(self):
        class Store:
            def used_memory(self):
                return 123456

        class Processor:
            node_location_storage = Store()

        self.assertEqual(location_cache_used_memory(Processor()), 123456)

    def test_worker_default_is_restored_to_four_thousand(self):
        self.assertEqual(TOPOLOGY_WORKER_BATCH_SIZE, 4_000)


if __name__ == "__main__":
    unittest.main()
