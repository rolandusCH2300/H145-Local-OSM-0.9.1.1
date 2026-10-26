from __future__ import annotations

import unittest
from unittest.mock import patch

import local_osm.db as db


class Schema5FusedDecodeTests(unittest.TestCase):
    def test_geometry_decode_is_fused_and_preserves_mixed_coordinates(self) -> None:
        source = [
            (1000, 60.1234567, 22.7654321),
            (1005, None, None),
            (998, 60.1234578, 22.7654300),
            (2**40 + 123, -33.8651430, 151.2099000),
        ]
        node_ids, coordinates = db.encode_way_geometry_v5(source)

        # Decode both packed streams directly without intermediate lists.
        with patch.object(
            db,
            "decode_way_node_ids",
            side_effect=AssertionError("full geometry must not materialize an ID list first"),
        ), patch.object(
            db,
            "decode_way_coordinates",
            side_effect=AssertionError("full geometry must not materialize a coordinate list first"),
        ):
            decoded = db.decode_way_geometry_v5(node_ids, coordinates)

        self.assertEqual([row[0] for row in decoded], [row[0] for row in source])
        for actual, expected in zip(decoded, source):
            self.assertEqual(actual[0], expected[0])
            if expected[1] is None:
                self.assertEqual(actual[1:], (None, None))
            else:
                self.assertAlmostEqual(float(actual[1]), float(expected[1]), places=7)
                self.assertAlmostEqual(float(actual[2]), float(expected[2]), places=7)

    def test_fused_decode_rejects_stream_length_mismatch(self) -> None:
        node_ids, _coordinates = db.encode_way_geometry_v5([
            (1, 60.0, 22.0),
            (2, 60.1, 22.1),
        ])
        _one_id, one_coordinate = db.encode_way_geometry_v5([
            (1, 60.0, 22.0),
        ])

        with self.assertRaisesRegex(ValueError, "stream length mismatch"):
            db.decode_way_geometry_v5(node_ids, one_coordinate)


if __name__ == "__main__":
    unittest.main()
