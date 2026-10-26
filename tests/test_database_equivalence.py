import tempfile
import unittest
from pathlib import Path

from local_osm.database_equivalence import compare_databases
from local_osm.db import OSMObject, connect, encode_way_geometry_v5, insert_object, insert_way_geometry_v5_rows
from local_osm.import_optimizations import insert_new_object_without_rtree, rebuild_object_rtree
from local_osm.importer import _create_bulk_load_indexes, _drop_bulk_load_indexes
from local_osm.tier_c import finalize_tier_c_tags, prepare_tier_c_schema


class DatabaseEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_path = root / "old.sqlite3"
        self.new_path = root / "new.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def _build_old(self):
        con = connect(self.old_path)
        try:
            con.execute("INSERT INTO metadata(key,value) VALUES('profile','h145-missions-v1')")
            con.execute("INSERT INTO metadata(key,value) VALUES('schema_version','5')")
            obj = OSMObject(
                "way", 100, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
                {"highway": "primary", "name": "Same road", "surface": "asphalt"}, False,
            )
            insert_object(con, obj)
            ids, coords = encode_way_geometry_v5([(1, 60.0, 24.0), (2, 60.1, 24.1)])
            insert_way_geometry_v5_rows(con, [(100, ids, coords)])
            # No packed cache on purpose: comparator must fall back to full object_tags.
            con.commit()
        finally:
            con.close()

    def _build_new(self, *, changed_name=False):
        con = connect(self.new_path)
        try:
            con.execute("PRAGMA foreign_keys=OFF")
            prepare_tier_c_schema(con)
            _drop_bulk_load_indexes(con)
            obj = OSMObject(
                "way", 100, 60.0, 24.0, 59.9, 60.1, 23.9, 24.1,
                {
                    "highway": "primary",
                    "name": "Changed road" if changed_name else "Same road",
                    "surface": "asphalt",
                },
                False,
            )
            # This helper intentionally leaves RTree maintenance deferred. The
            # production hot batch keeps it inline; complete the fixture's RTree
            # explicitly so the production verifier remains fail-closed.
            insert_new_object_without_rtree(con, obj)
            rebuild_object_rtree(con)
            ids, coords = encode_way_geometry_v5([(1, 60.0, 24.0), (2, 60.1, 24.1)])
            insert_way_geometry_v5_rows(con, [(100, ids, coords)])
            _create_bulk_load_indexes(con)
            finalize_tier_c_tags(con)
            con.execute("INSERT INTO metadata(key,value) VALUES('profile','h145-missions-v1')")
            con.execute("INSERT INTO metadata(key,value) VALUES('schema_version','6')")
            con.commit()
        finally:
            con.close()

    def test_equivalent_semantics_pass_despite_different_internal_primary_keys(self):
        self._build_old()
        self._build_new()
        stats = compare_databases(self.old_path, self.new_path)
        self.assertEqual(stats.objects, 1)
        self.assertEqual(stats.way_geometries, 1)
        self.assertEqual(stats.full_tags, 3)
        self.assertEqual(stats.selector_tags, 1)

    def test_full_tag_difference_is_detected(self):
        self._build_old()
        self._build_new(changed_name=True)
        with self.assertRaises(RuntimeError):
            compare_databases(self.old_path, self.new_path)


if __name__ == "__main__":
    unittest.main()
