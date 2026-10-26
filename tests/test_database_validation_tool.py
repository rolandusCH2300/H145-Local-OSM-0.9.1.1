from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_osm.database_validation import validate_database
from local_osm.db import connect, seed_demo
from local_osm.importer import prepare_tier_c_schema
from local_osm.mission_audit import audit_occurrences, scan_mission_queries


class DatabaseValidationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.sqlite3"
        self.con = connect(self.db)
        seed_demo(self.con)
        self.con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','5')"
        )
        self.con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('profile','h145-missions-v1')"
        )
        self.con.commit()
        self.con.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_mission(self, query: str) -> Path:
        mission = self.root / "Community" / "pkg" / "hpgmission" / "mission.json"
        mission.parent.mkdir(parents=True, exist_ok=True)
        mission.write_text(json.dumps({"query": query}), encoding="utf-8")
        return mission.parent.parents[1]

    def test_validation_uses_explicit_database_and_quick_check(self) -> None:
        report = validate_database(self.db, require_derived=False)

        self.assertTrue(report.ok)
        self.assertEqual(report.database, str(self.db.resolve()))
        self.assertEqual(report.quick_check, "ok")
        self.assertGreater(report.counts["objects"], 0)
        self.assertTrue(any(c.name == "schema_version" and c.ok for c in report.checks))
        self.assertTrue(any(c.name == "object_rtree_row_count" and c.ok for c in report.checks))

    def test_validation_accepts_finalized_schema_v6_core_layout(self) -> None:
        db = self.root / "schema-v6.sqlite3"
        con = connect(db)
        try:
            con.execute("PRAGMA foreign_keys=OFF")
            prepare_tier_c_schema(con)
            con.execute(
                """INSERT INTO objects
                   (pk,osm_type,osm_id,center_lat,center_lon,min_lat,max_lat,min_lon,max_lon,area_eligible,has_tags)
                   VALUES(1,'way',100,60.0,24.0,59.9,60.1,23.9,24.1,0,1)"""
            )
            con.execute(
                "INSERT INTO object_rtree(pk,min_lon,max_lon,min_lat,max_lat) VALUES(1,23.9,24.1,59.9,60.1)"
            )
            con.execute(
                "INSERT INTO object_tags(object_pk,key,value) VALUES(1,'highway','service')"
            )
            con.execute(
                "INSERT INTO way_geometry_v5(way_id,node_ids,coordinates) VALUES(100,?,?)",
                (b"ids", b"coords"),
            )
            con.executemany(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                (
                    ("schema_version", "6"),
                    ("profile", "h145-missions-v1"),
                ),
            )
            con.commit()
        finally:
            con.close()

        report = validate_database(db, require_derived=False)
        self.assertTrue(report.ok)
        self.assertTrue(any(c.name == "schema_version" and c.ok and c.actual == 6 for c in report.checks))
        self.assertEqual(report.counts["way_geometry_v5"], 1)

    def test_validation_fails_when_object_rtree_is_incomplete(self) -> None:
        con = connect(self.db)
        pk = int(con.execute("SELECT pk FROM objects ORDER BY pk LIMIT 1").fetchone()[0])
        con.execute("DELETE FROM object_rtree WHERE pk=?", (pk,))
        con.commit()
        con.close()

        report = validate_database(self.db, require_derived=False)

        self.assertFalse(report.ok)
        check = next(c for c in report.checks if c.name == "object_rtree_row_count")
        self.assertFalse(check.ok)
        self.assertEqual(check.actual, report.counts["object_rtree"])
        self.assertEqual(check.expected, report.counts["objects"])

    def test_validation_detects_mission_profile_hash_mismatch(self) -> None:
        packages_root = self._write_mission(
            "[out:json];nwr({{bbox}})[amenity=hospital];out center;"
        )
        scan = scan_mission_queries(packages_root)
        audit = audit_occurrences(scan)

        con = connect(self.db)
        con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('mission_profile_hash',?)",
            ("0" * 64,),
        )
        con.execute(
            "INSERT OR REPLACE INTO metadata(key,value) VALUES('mission_unique_queries',?)",
            (str(audit.unique_queries),),
        )
        con.commit()
        con.close()

        report = validate_database(
            self.db,
            packages_root=packages_root,
            require_derived=False,
        )

        self.assertFalse(report.ok)
        mismatch = next(c for c in report.checks if c.name == "mission_profile_hash")
        self.assertFalse(mismatch.ok)
        self.assertEqual(mismatch.actual, "0" * 64)
        self.assertEqual(mismatch.expected, audit.mission_profile_hash)


if __name__ == "__main__":
    unittest.main()
