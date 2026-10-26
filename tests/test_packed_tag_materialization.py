from __future__ import annotations

import unittest
from unittest.mock import patch

from local_osm import hydration_cache


class _NoStr(str):
    def __str__(self) -> str:
        raise AssertionError("packed tag decoder must not re-coerce decoded strings")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _RecordingConnection:
    def __init__(self) -> None:
        self.batches: list[tuple[int, ...]] = []

    def execute(self, _sql: str, params):
        batch = tuple(params)
        self.batches.append(batch)
        rows = [(pk, b'{}') for pk in batch]
        return _Rows(rows)


class PackedTagMaterializationTests(unittest.TestCase):
    def test_decode_returns_json_dict_without_second_string_coercion(self) -> None:
        decoded = {_NoStr("amenity"): _NoStr("parking")}
        with patch("local_osm.hydration_cache.json.loads", return_value=decoded):
            result = hydration_cache._decode_tags(b"ignored")
        self.assertIs(result, decoded)

    def test_multichunk_fetches_primary_keys_in_locality_order(self) -> None:
        con = _RecordingConnection()
        with patch("local_osm.hydration_cache.sqlite_in_chunk_size", return_value=2):
            result, _sql_ms, _decode_ms = hydration_cache.load_packed_tags_many(
                con, [30, 10, 20, 10]
            )

        self.assertEqual(con.batches, [(10, 20), (30,)])
        # The public mapping still preserves the caller's de-duplicated logical
        # order even though physical fetches were reordered.
        self.assertEqual(list(result), [30, 10, 20])

    def test_single_chunk_preserves_input_fetch_order(self) -> None:
        con = _RecordingConnection()
        with patch("local_osm.hydration_cache.sqlite_in_chunk_size", return_value=8):
            result, _sql_ms, _decode_ms = hydration_cache.load_packed_tags_many(
                con, [30, 10, 20, 10]
            )

        self.assertEqual(con.batches, [(30, 10, 20)])
        self.assertEqual(list(result), [30, 10, 20])


if __name__ == "__main__":
    unittest.main()
