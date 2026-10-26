from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from local_osm.mission_audit import MissionScan

from local_osm.multibuild import (
    _mission_audit_metadata,
    _run_mission_audit_preflight,
    _selected_country_iso2,
    bundle_slug,
    find_osmium_executable,
    merge_pbfs,
    merge_sorted_streams,
    resolve_regions,
    validate_snapshot_signatures,
)


class FakeObject:
    def __init__(self, kind: str, osm_id: int, version: int = 1):
        self.kind = kind
        self.id = osm_id
        self.version = version

    def is_node(self) -> bool:
        return self.kind == "node"

    def is_way(self) -> bool:
        return self.kind == "way"

    def is_relation(self) -> bool:
        return self.kind == "relation"


class FakeWriter:
    def __init__(self):
        self.rows: list[tuple[str, int, int]] = []

    def add(self, obj: FakeObject) -> None:
        self.rows.append((obj.kind, int(obj.id), int(obj.version)))


class MultiBuildTests(unittest.TestCase):
    def test_resolve_regions_deduplicates_same_region_preserving_order(self) -> None:
        index = {
            "features": [
                {
                    "properties": {
                        "id": "finland",
                        "name": "Finland",
                        "iso3166-1:alpha2": ["FI"],
                        "urls": {"pbf": "https://example.invalid/finland.osm.pbf"},
                    }
                },
                {
                    "properties": {
                        "id": "sweden",
                        "name": "Sweden",
                        "iso3166-1:alpha2": ["SE"],
                        "urls": {"pbf": "https://example.invalid/sweden.osm.pbf"},
                    }
                },
            ]
        }

        regions = resolve_regions(index, ["Finland", "SE", "FI"])

        self.assertEqual([row["id"] for row in regions], ["finland", "sweden"])

    def test_selected_country_iso2_uses_only_explicit_region_codes(self) -> None:
        regions = [
            {"id": "finland", "iso2": ["FI"]},
            {"id": "norway", "iso2": ["NO"]},
            {"id": "custom-region", "iso2": []},
            {"id": "duplicate-norway", "iso2": ["no"]},
        ]

        self.assertEqual(_selected_country_iso2(regions), ("FI", "NO"))

    def test_mission_audit_metadata_is_deterministic(self) -> None:
        result = SimpleNamespace(
            profile_name="h145-missions-v1",
            mission_profile_hash="abc123",
            packages=("pkg-b", "pkg-a"),
            packages_root=r"C:\\MSFS\\Community",
            files_scanned=43,
            files_with_osm=7,
            query_occurrences=148,
            unique_queries=95,
            selector_branches=208,
        )

        metadata = _mission_audit_metadata(result)

        self.assertEqual(metadata["mission_profile_name"], "h145-missions-v1")
        self.assertEqual(metadata["mission_profile_hash"], "abc123")
        self.assertEqual(metadata["mission_query_occurrences"], "148")
        self.assertEqual(metadata["mission_unique_queries"], "95")
        self.assertEqual(metadata["mission_selector_branches"], "208")
        self.assertEqual(metadata["mission_files_scanned"], "43")
        self.assertEqual(metadata["mission_files_with_osm"], "7")
        self.assertEqual(
            json.loads(metadata["mission_packages"]),
            ["pkg-a", "pkg-b"],
        )
        self.assertEqual(metadata["mission_packages_root"], r"C:\\MSFS\\Community")

    def test_mission_audit_preflight_fails_closed(self) -> None:
        result = SimpleNamespace(
            ok=False,
            query_occurrences=10,
            unique_queries=8,
            selector_branches=12,
            covered_branches=11,
            uncovered_branches=1,
            missing_requirements=[SimpleNamespace(mode="any", key="historic", values=())],
            parse_errors=[],
            scan_errors=[],
            mission_profile_hash="bad",
        )

        with (
            mock.patch("local_osm.multibuild.scan_mission_queries", return_value=MissionScan("Community")),
            mock.patch("local_osm.multibuild.audit_occurrences", return_value=result),
        ):
            with self.assertRaisesRegex(ValueError, r"mission audit failed.*historic"):
                _run_mission_audit_preflight(r"C:\\MSFS\\Community")

    def test_mission_audit_preflight_returns_passing_profile(self) -> None:
        result = SimpleNamespace(
            ok=True,
            query_occurrences=148,
            unique_queries=95,
            selector_branches=208,
            covered_branches=208,
            uncovered_branches=0,
            missing_requirements=[],
            parse_errors=[],
            scan_errors=[],
            mission_profile_hash="ec53b09c",
        )

        with (
            mock.patch("local_osm.multibuild.scan_mission_queries", return_value=MissionScan("Community")),
            mock.patch("local_osm.multibuild.audit_occurrences", return_value=result),
        ):
            self.assertIs(
                _run_mission_audit_preflight(r"C:\\MSFS\\Community"),
                result,
            )

    def test_bundle_slug_is_stable_filesystem_safe_and_order_independent(self) -> None:
        regions = [
            {"id": "europe/finland", "name": "Finland"},
            {"id": "europe/sweden", "name": "Sweden"},
        ]

        self.assertEqual(bundle_slug(regions), "finland-sweden")
        self.assertEqual(bundle_slug(list(reversed(regions))), "finland-sweden")

    def test_bundled_osmium_is_preferred_over_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled = root / "tools" / "osmium" / "osmium.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"stub")

            with mock.patch(
                "local_osm.multibuild.shutil.which",
                return_value=r"C:\\system\\osmium.exe",
            ):
                self.assertEqual(
                    find_osmium_executable(project_root=root),
                    str(bundled.resolve()),
                )

    def test_osmium_path_fallback_when_bundle_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            system_osmium = r"C:\\system\\osmium.exe"
            with mock.patch(
                "local_osm.multibuild.shutil.which",
                return_value=system_osmium,
            ):
                self.assertEqual(
                    find_osmium_executable(project_root=Path(tmp)),
                    system_osmium,
                )

    def test_snapshot_validation_accepts_shared_timestamp_with_different_sequences(self) -> None:
        timestamp = "2026-08-20T00:00:00+00:00"

        self.assertEqual(
            validate_snapshot_signatures(
                [
                    ("123456", timestamp),
                    ("987654", timestamp),
                    (None, timestamp),
                ]
            ),
            timestamp,
        )

    def test_snapshot_validation_rejects_different_replication_snapshots(self) -> None:
        with self.assertRaisesRegex(ValueError, "different replication snapshots"):
            validate_snapshot_signatures(
                [
                    ("123456", "2026-08-20T00:00:00+00:00"),
                    ("123457", "2026-08-20T00:01:00+00:00"),
                ]
            )

    def test_snapshot_validation_rejects_missing_replication_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "could not be verified"):
            validate_snapshot_signatures(
                [
                    ("123456", "2026-08-20T00:00:00+00:00"),
                    ("123457", None),
                ]
            )

    def test_local_merge_allows_missing_replication_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.osm.pbf"
            second = root / "second.osm.pbf"
            output = root / "merged.osm.pbf"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            def fake_merge(_osmium, _paths, target):
                target.write_bytes(b"merged")
                return {
                    "engine": "python",
                    "input_objects": None,
                    "output_objects": None,
                    "duplicates_removed": None,
                }

            with (
                mock.patch(
                    "local_osm.multibuild._require_osmium_module",
                    return_value=object(),
                ),
                mock.patch(
                    "local_osm.multibuild._replication_signature",
                    side_effect=[None, ("123456", None)],
                ),
                mock.patch(
                    "local_osm.multibuild.find_osmium_executable",
                    return_value=None,
                ),
                mock.patch(
                    "local_osm.multibuild._merge_with_python",
                    side_effect=fake_merge,
                ),
            ):
                stats = merge_pbfs(
                    [first, second],
                    output,
                    engine="python",
                    require_snapshot_consistency=False,
                )

            self.assertIsNone(stats["same_replication_snapshot"])
            self.assertIsNone(stats["replication_timestamp"])
            self.assertEqual(stats["snapshot_verification"], "not-enforced")
            self.assertTrue(output.is_file())

    def test_local_merge_reports_different_timestamps_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.osm.pbf"
            second = root / "second.osm.pbf"
            output = root / "merged.osm.pbf"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            def fake_merge(_osmium, _paths, target):
                target.write_bytes(b"merged")
                return {
                    "engine": "python",
                    "input_objects": None,
                    "output_objects": None,
                    "duplicates_removed": None,
                }

            with (
                mock.patch(
                    "local_osm.multibuild._require_osmium_module",
                    return_value=object(),
                ),
                mock.patch(
                    "local_osm.multibuild._replication_signature",
                    side_effect=[
                        ("1", "2026-08-20T00:00:00+00:00"),
                        ("2", "2026-08-21T00:00:00+00:00"),
                    ],
                ),
                mock.patch(
                    "local_osm.multibuild.find_osmium_executable",
                    return_value=None,
                ),
                mock.patch(
                    "local_osm.multibuild._merge_with_python",
                    side_effect=fake_merge,
                ),
            ):
                stats = merge_pbfs(
                    [first, second],
                    output,
                    engine="python",
                    require_snapshot_consistency=False,
                )

            self.assertFalse(stats["same_replication_snapshot"])
            self.assertIsNone(stats["replication_timestamp"])
            self.assertEqual(stats["snapshot_verification"], "not-enforced")

    def test_preflight_allows_missing_pbf_replication_headers(self) -> None:
        timestamp = "2026-08-20T00:21:03Z"

        self.assertEqual(
            validate_snapshot_signatures(
                [None, ("123456", None)],
                expected_timestamp=timestamp,
            ),
            timestamp,
        )

    def test_preflight_rejects_conflicting_pbf_header_when_present(self) -> None:
        with self.assertRaisesRegex(ValueError, "disagrees with the Geofabrik preflight"):
            validate_snapshot_signatures(
                [
                    None,
                    ("123456", "2026-08-19T00:21:03+00:00"),
                ],
                expected_timestamp="2026-08-20T00:21:03Z",
            )

    def test_merge_sorted_streams_deduplicates_overlap_using_newest_version(self) -> None:
        streams = [
            iter(
                [
                    FakeObject("node", 1, 1),
                    FakeObject("node", 3, 1),
                    FakeObject("way", 10, 1),
                    FakeObject("relation", 100, 1),
                ]
            ),
            iter(
                [
                    FakeObject("node", 1, 2),
                    FakeObject("node", 2, 1),
                    FakeObject("way", 10, 1),
                    FakeObject("way", 11, 1),
                    FakeObject("relation", 101, 1),
                ]
            ),
        ]
        writer = FakeWriter()

        stats = merge_sorted_streams(streams, writer)

        self.assertEqual(
            writer.rows,
            [
                ("node", 1, 2),
                ("node", 2, 1),
                ("node", 3, 1),
                ("way", 10, 1),
                ("way", 11, 1),
                ("relation", 100, 1),
                ("relation", 101, 1),
            ],
        )
        self.assertEqual(stats["input_objects"], 9)
        self.assertEqual(stats["output_objects"], 7)
        self.assertEqual(stats["duplicates_removed"], 2)

    def test_merge_sorted_streams_rejects_unsorted_input(self) -> None:
        streams = [
            iter(
                [
                    FakeObject("node", 2),
                    FakeObject("node", 1),
                ]
            )
        ]

        with self.assertRaisesRegex(ValueError, "not sorted"):
            merge_sorted_streams(streams, FakeWriter())


if __name__ == "__main__":
    unittest.main()
