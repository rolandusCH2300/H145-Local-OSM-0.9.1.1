from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

import local_osm.db as db
import local_osm.query as query


class _RecordingConnection:
    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.way_geometry_v5_batches: list[tuple[int, ...]] = []

    def execute(self, sql: str, params=()):
        if "FROM way_geometry_v5" in sql:
            self.way_geometry_v5_batches.append(tuple(int(v) for v in params))
        return self.con.execute(sql, params)


class _NoIntNode(int):
    """Regression sentinel: B3 must not re-coerce decoded node IDs."""

    def __int__(self):
        raise AssertionError("decoded way node ID was redundantly coerced with int()")


class WayGeometryLocalityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:")
        self.con.execute(
            "CREATE TABLE way_geometry_v5("
            "way_id INTEGER PRIMARY KEY,"
            "node_ids BLOB NOT NULL,"
            "coordinates BLOB NOT NULL"
            ") WITHOUT ROWID"
        )
        self.expected: dict[int, list[tuple[int, float, float]]] = {}
        for way_id in (10, 20, 30):
            geometry = [
                (way_id * 100 + 1, 60.0 + way_id / 1000.0, 22.0 + way_id / 1000.0),
                (way_id * 100 + 2, 60.1 + way_id / 1000.0, 22.1 + way_id / 1000.0),
            ]
            self.expected[way_id] = geometry
            node_ids, coordinates = db.encode_way_geometry_v5(geometry)
            self.con.execute(
                "INSERT INTO way_geometry_v5(way_id,node_ids,coordinates) VALUES(?,?,?)",
                (way_id, node_ids, coordinates),
            )

    def tearDown(self) -> None:
        self.con.close()

    def _assert_geometry(self, loaded) -> None:
        self.assertEqual(list(loaded), [30, 10, 20])
        for way_id in (30, 10, 20):
            actual = loaded[way_id]
            wanted = self.expected[way_id]
            self.assertEqual([row[0] for row in actual], [row[0] for row in wanted])
            for actual_row, wanted_row in zip(actual, wanted):
                self.assertAlmostEqual(float(actual_row[1]), wanted_row[1], places=7)
                self.assertAlmostEqual(float(actual_row[2]), wanted_row[2], places=7)

    def test_multibatch_full_geometry_fetch_is_locality_sorted(self) -> None:
        recording = _RecordingConnection(self.con)
        with patch.object(query, "sqlite_in_chunk_size", return_value=2):
            loaded, _sql_ms, _decode_ms = query._load_way_geometry_profiled(
                recording,
                [30, 10, 20, 10],
            )

        self.assertEqual(recording.way_geometry_v5_batches, [(10, 20), (30,)])
        self._assert_geometry(loaded)

    def test_single_batch_keeps_existing_request_order(self) -> None:
        recording = _RecordingConnection(self.con)
        with patch.object(query, "sqlite_in_chunk_size", return_value=8):
            loaded, _sql_ms, _decode_ms = query._load_way_geometry_profiled(
                recording,
                [30, 10, 20, 10],
            )

        self.assertEqual(recording.way_geometry_v5_batches, [(30, 10, 20)])
        self._assert_geometry(loaded)

    def test_preload_way_geometry_reuses_decoded_node_ids_without_coercion(self) -> None:
        geometry = [
            (_NoIntNode(101), 60.0, 22.0),
            (_NoIntNode(102), 60.1, 22.1),
        ]
        with patch.object(
            query,
            "_hydration_cache_flags",
            return_value=(False, False),
        ), patch.object(
            query,
            "_load_way_geometry_profiled",
            return_value=({7: geometry}, 0.0, 0.0),
        ):
            loader = query._ElementLoader(self.con)
            loader.preload_way_geometry([7])

        self.assertIs(loader.way_nodes[7][0], geometry[0][0])
        self.assertIs(loader.way_nodes[7][1], geometry[1][0])
        self.assertEqual(loader.way_nodes[7], [101, 102])


if __name__ == "__main__":
    unittest.main()
