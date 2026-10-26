from __future__ import annotations

import unittest

from local_osm import geofabrik
from local_osm.multibuild import validate_preflight_snapshots


class MultiBuildPreflightTests(unittest.TestCase):
    def test_region_records_preserve_updates_url_for_preflight(self) -> None:
        index = {
            "features": [
                {
                    "properties": {
                        "id": "finland",
                        "name": "Finland",
                        "iso3166-1:alpha2": ["FI"],
                        "urls": {
                            "pbf": "https://example.invalid/finland-latest.osm.pbf",
                            "updates": "https://example.invalid/finland-updates",
                        },
                    }
                }
            ]
        }

        records = geofabrik.region_records(index)

        self.assertEqual(records[0]["updates"], "https://example.invalid/finland-updates")

    def test_parse_update_state_unescapes_osmosis_timestamp(self) -> None:
        state = geofabrik.parse_update_state(
            "sequenceNumber=123456\n"
            "timestamp=2026-08-20T00\\:21\\:03Z\n"
        )

        self.assertEqual(state["sequenceNumber"], "123456")
        self.assertEqual(state["timestamp"], "2026-08-20T00:21:03Z")

    def test_preflight_accepts_shared_timestamp_even_if_sequences_differ(self) -> None:
        timestamp = "2026-08-20T00:21:03Z"
        snapshots = [
            {
                "id": "finland",
                "name": "Finland",
                "timestamp": timestamp,
                "sequence": "111",
                "md5": "a" * 32,
            },
            {
                "id": "norway",
                "name": "Norway",
                "timestamp": timestamp,
                "sequence": "999",
                "md5": "b" * 32,
            },
        ]

        self.assertEqual(validate_preflight_snapshots(snapshots), timestamp)

    def test_preflight_rejects_different_timestamps_before_download(self) -> None:
        snapshots = [
            {
                "id": "finland",
                "name": "Finland",
                "timestamp": "2026-08-20T00:21:03Z",
                "sequence": "111",
                "md5": "a" * 32,
            },
            {
                "id": "norway",
                "name": "Norway",
                "timestamp": "2026-08-19T00:21:03Z",
                "sequence": "999",
                "md5": "b" * 32,
            },
        ]

        with self.assertRaisesRegex(ValueError, "before download"):
            validate_preflight_snapshots(snapshots)


if __name__ == "__main__":
    unittest.main()
