from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_osm.db import connect
from local_osm import importer, road_index


class FinalizedStorageLayoutTests(unittest.TestCase):
    def test_schema_v6_uses_finalized_table_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "layout.sqlite3")
            try:
                con.execute("PRAGMA foreign_keys=OFF")
                importer.prepare_tier_c_schema(con)
                sql = {
                    row[0]: (row[1] or "").upper()
                    for row in con.execute(
                        "SELECT name,sql FROM sqlite_master "
                        "WHERE type='table' AND name IN "
                        "('way_geometry_v5','node_coords','object_tags','packed_object_tags')"
                    )
                }
                self.assertNotIn("WITHOUT ROWID", sql["way_geometry_v5"])
                self.assertNotIn("WITHOUT ROWID", sql["packed_object_tags"])
                self.assertIn("WITHOUT ROWID", sql["node_coords"])
                self.assertIn("WITHOUT ROWID", sql["object_tags"])

                con.execute(
                    "INSERT INTO way_geometry_v5(way_id,node_ids,coordinates) VALUES(?,?,?)",
                    (123456789, b"ids", b"coords"),
                )
                row = con.execute(
                    "SELECT rowid,way_id FROM way_geometry_v5 WHERE way_id=?",
                    (123456789,),
                ).fetchone()
                self.assertEqual(tuple(row), (123456789, 123456789))

                trigger = con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='protect_tier_c_packed_tags_delete'"
                ).fetchone()
                self.assertIsNotNone(trigger)
            finally:
                con.close()

    def test_road_ways_is_canonical_integer_primary_key_rowid_table(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(road_index.ROAD_INDEX_DDL)
            sql = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='road_ways'"
            ).fetchone()[0]
            self.assertNotIn("WITHOUT ROWID", sql.upper())

            con.execute(
                "INSERT INTO road_ways(way_id,road_class,node_ids_fixed) VALUES(?,?,?)",
                (42, 3, b"abc"),
            )
            row = con.execute(
                "SELECT rowid,way_id,road_class,node_ids_fixed FROM road_ways"
            ).fetchone()
            self.assertEqual(row, (42, 42, 3, b"abc"))

            con.execute(
                "UPDATE road_ways SET node_ids_fixed=? WHERE way_id=?",
                (b"def", 42),
            )
            self.assertEqual(
                con.execute(
                    "SELECT node_ids_fixed FROM road_ways WHERE way_id=?", (42,)
                ).fetchone()[0],
                b"def",
            )

            con.execute(
                "INSERT INTO road_way_rtree(way_id,min_lon,max_lon,min_lat,max_lat) "
                "VALUES(?,?,?,?,?)",
                (42, 24.0, 25.0, 60.0, 61.0),
            )
            self.assertEqual(
                int(con.execute("SELECT way_id FROM road_way_rtree").fetchone()[0]),
                42,
            )
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
