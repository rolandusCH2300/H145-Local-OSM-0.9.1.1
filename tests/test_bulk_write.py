import sqlite3
import unittest

from local_osm.bulk_write import (
    WriteOptions, WriteProfile, direct_insert, discard_write_options,
    grouped_insert, insert_geometry_rows, insert_object_rows,
    make_write_options, register_write_options,
)
from local_osm.tier_c import TIER_C_OBJECT_SCHEMA, WAY_GEOMETRY_V5_SCHEMA


class GroupedInsertTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE sample(pk INTEGER PRIMARY KEY, label TEXT, data BLOB)")

    def tearDown(self):
        self.con.close()

    def test_variable_limit_partial_groups_and_exact_values(self):
        self.con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 15)
        rows = [(2**53 + i, "Åland '); -- " + str(i), bytes([i % 256, 0, 255]))
                for i in range(137)]
        profile = WriteProfile()
        grouped_insert(self.con, "INSERT INTO sample(pk,label,data)", 3, iter(rows),
                       sql_rows=64, profile=profile)
        self.assertEqual(self.con.execute("SELECT * FROM sample ORDER BY pk").fetchall(), rows)
        self.assertEqual(profile.object_rows, 137)
        self.assertEqual(profile.sql_statements, 28)
        self.assertTrue(self.con.in_transaction)  # caller owns commit/rollback
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)

    def test_blob_boundaries_and_oversized_single_row(self):
        rows = [(i, str(i), bytes([i]) * size)
                for i, size in enumerate([4, 4, 4, 20, 2, 2])]
        profile = WriteProfile()
        grouped_insert(self.con, "INSERT INTO sample(pk,label,data)", 3, rows,
                       sql_rows=64, blob_columns=(2,), max_statement_bytes=8,
                       profile=profile, kind="geometry")
        self.assertEqual(self.con.execute("SELECT * FROM sample ORDER BY pk").fetchall(), rows)
        self.assertEqual(profile.sql_statements, 4)
        self.assertEqual(profile.geometry_bytes, 36)

    def test_multiple_packing_blocks_and_empty_input(self):
        rows = [(i, None, b"\x00\xff") for i in range(2051)]
        profile = WriteProfile()
        grouped_insert(self.con, "INSERT INTO sample", 3, rows, profile=profile)
        grouped_insert(self.con, "INSERT INTO sample", 3, [], profile=profile)
        self.assertEqual(self.con.execute("SELECT * FROM sample ORDER BY pk").fetchall(), rows)
        self.assertEqual(profile.sql_statements, 33)

    def test_constraint_failure_propagates_and_caller_can_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            grouped_insert(self.con, "INSERT INTO sample", 3,
                           [(1, "first", b"a"), (2, "second", b"b"), (1, "duplicate", b"c")],
                           sql_rows=2)
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)

    def test_invalid_limits_fail_before_inserting(self):
        for args in [(0, 4), (1025, 4), (64, 0)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                make_write_options(*args)
        self.assertEqual(WriteOptions().sql_rows, 64)
        with self.assertRaises(ValueError):
            make_write_options(sql_write_mode="unknown")


class RecordingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.insert_calls = []

    def executemany(self, sql, rows):
        self.insert_calls.append((sql, rows))
        return super().executemany(sql, rows)


class DirectInsertTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:", factory=RecordingConnection)
        self.con.execute("CREATE TABLE sample(pk INTEGER PRIMARY KEY, label TEXT, data BLOB)")

    def tearDown(self):
        discard_write_options(self.con)
        self.con.close()

    def test_whole_batch_is_passed_to_one_call_without_packing(self):
        self.con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
        rows = [(2**53 + i, "Åland '); --", b"\x00\xff") for i in range(2051)]
        profile = WriteProfile()
        direct_insert(self.con, "INSERT INTO sample", 3, rows, profile=profile)
        self.assertEqual(len(self.con.insert_calls), 1)
        self.assertIs(self.con.insert_calls[0][1], rows)
        self.assertEqual(self.con.execute("SELECT * FROM sample ORDER BY pk").fetchall(), rows)
        self.assertEqual(profile.object_rows, len(rows))
        self.assertEqual(profile.sql_statements, len(rows))
        self.assertEqual(profile.executemany_calls, 1)
        self.assertEqual(profile.packing_seconds, 0)
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)

    def test_empty_batch_and_failure_preserve_caller_transaction_ownership(self):
        profile = WriteProfile()
        direct_insert(self.con, "INSERT INTO sample", 3, [], profile=profile)
        self.assertEqual(self.con.insert_calls, [])
        with self.assertRaises(sqlite3.IntegrityError):
            direct_insert(self.con, "INSERT INTO sample", 3,
                          [(1, "first", b"a"), (2, "second", b"b"), (1, "duplicate", b"c")],
                          profile=profile)
        self.assertTrue(self.con.in_transaction)
        self.con.rollback()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
        self.assertEqual(profile.object_rows, 0)
        self.assertGreater(profile.object_sql_seconds, 0)

    def test_selected_direct_mode_covers_objects_and_large_geometry(self):
        self.con.executescript(TIER_C_OBJECT_SCHEMA + ";" + WAY_GEOMETRY_V5_SCHEMA)
        options = make_write_options(128, 16000, "direct")
        register_write_options(self.con, options)
        self.assertEqual(options.effective_sql_rows, 1)
        objects = [(i, "way", 2**53 + i, 60., 24., 60., 60., 24., 24., 0, 0)
                   for i in range(1, 2052)]
        geometries = [(2**53 + i, b"\x00\xff", b"\x01\x00") for i in range(1, 2052)]
        geometries[-1] = (geometries[-1][0], b"\x00" * (1024**2 + 1), b"\xff")
        profile = WriteProfile()
        insert_object_rows(self.con, objects, profile=profile)
        insert_geometry_rows(self.con, geometries, profile=profile)
        self.assertEqual(len(self.con.insert_calls), 2)
        self.assertIs(self.con.insert_calls[0][1], objects)
        # Geometry uses the old per-row int/Binary adapter, still in one call.
        self.assertIsNot(self.con.insert_calls[1][1], geometries)
        self.assertEqual(self.con.execute("SELECT * FROM objects ORDER BY pk").fetchall(), objects)
        self.assertEqual(self.con.execute("SELECT * FROM way_geometry_v5 ORDER BY way_id").fetchall(), geometries)
        self.assertEqual(profile.geometry_bytes, sum(len(r[1]) + len(r[2]) for r in geometries))
        self.assertEqual(profile.object_rows, 2051)
        self.assertEqual(profile.geometry_rows, 2051)
        self.assertEqual(profile.executemany_calls, 2)
        self.assertEqual(profile.sql_statements, 4102)
        self.assertEqual(profile.packing_seconds, 0)

    def test_grouped_single_row_still_uses_packing_blocks(self):
        rows = [(i, "label", b"data") for i in range(2051)]
        profile = WriteProfile()
        grouped_insert(self.con, "INSERT INTO sample", 3, rows, sql_rows=1, profile=profile)
        self.assertEqual(len(self.con.insert_calls), 3)
        self.assertEqual(profile.executemany_calls, 3)
        self.assertEqual(profile.sql_statements, 2051)


if __name__ == "__main__":
    unittest.main()
