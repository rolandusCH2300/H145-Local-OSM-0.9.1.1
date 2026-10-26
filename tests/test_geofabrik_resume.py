from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_osm.geofabrik import prepare_resumable_download


class GeofabrikResumeTests(unittest.TestCase):
    def test_legacy_partial_without_snapshot_identity_is_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "finland-latest.osm.pbf"
            destination.write_bytes(b"old-partial")

            existing = prepare_resumable_download(
                "https://example.invalid/finland-latest.osm.pbf",
                destination,
                expected_size=100,
                resume_identity="new-snapshot-md5",
            )

            self.assertEqual(existing, 0)
            self.assertFalse(destination.exists())
            state = json.loads(
                Path(str(destination) + ".download.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["resume_identity"], "new-snapshot-md5")

    def test_partial_from_different_snapshot_is_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "finland-latest.osm.pbf"
            state_path = Path(str(destination) + ".download.json")
            destination.write_bytes(b"old-partial")
            state_path.write_text(
                json.dumps(
                    {
                        "url": "https://example.invalid/finland-latest.osm.pbf",
                        "expected_size": 100,
                        "resume_identity": "old-snapshot-md5",
                    }
                ),
                encoding="utf-8",
            )

            existing = prepare_resumable_download(
                "https://example.invalid/finland-latest.osm.pbf",
                destination,
                expected_size=101,
                resume_identity="new-snapshot-md5",
            )

            self.assertEqual(existing, 0)
            self.assertFalse(destination.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["expected_size"], 101)
            self.assertEqual(state["resume_identity"], "new-snapshot-md5")

    def test_matching_snapshot_partial_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "finland-latest.osm.pbf"
            state_path = Path(str(destination) + ".download.json")
            destination.write_bytes(b"partial")
            state_path.write_text(
                json.dumps(
                    {
                        "url": "https://example.invalid/finland-latest.osm.pbf",
                        "expected_size": 100,
                        "resume_identity": "same-snapshot-md5",
                    }
                ),
                encoding="utf-8",
            )

            existing = prepare_resumable_download(
                "https://example.invalid/finland-latest.osm.pbf",
                destination,
                expected_size=100,
                resume_identity="same-snapshot-md5",
            )

            self.assertEqual(existing, len(b"partial"))
            self.assertEqual(destination.read_bytes(), b"partial")


if __name__ == "__main__":
    unittest.main()
