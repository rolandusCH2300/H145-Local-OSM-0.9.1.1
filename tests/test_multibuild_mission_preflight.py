from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from local_osm.gui.services import _audit_for_local_build

from local_osm.multibuild import (
    _run_mission_audit_preflight,
    _mission_audit_metadata,
    command_build,
    write_bundle_metadata,
)


class MissionAuditBuildIntegrationTests(unittest.TestCase):
    def test_build_applies_exclusions_before_fetching_regions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "pack" / "hpgmission" / "broken.json"
            broken.parent.mkdir(parents=True)
            broken.write_text('{', encoding="utf-8")
            args = SimpleNamespace(packages_root=str(root), exclude_mission_file=[str(broken.resolve())])
            with mock.patch("local_osm.multibuild.geofabrik.fetch_index", side_effect=RuntimeError("reached region fetch")) as fetch:
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "reached region fetch"):
                    command_build(args)
            fetch.assert_called_once()

    def test_exclusions_skip_only_approved_files_in_both_build_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            missions = root / "pack" / "hpgmission"
            missions.mkdir(parents=True)
            broken = missions / "broken.json"
            broken.write_text('{', encoding="utf-8")
            valid = missions / "valid.json"
            valid.write_text('{}', encoding="utf-8")
            for preflight in (_run_mission_audit_preflight, _audit_for_local_build):
                with self.subTest(preflight=preflight.__name__), redirect_stdout(io.StringIO()):
                    result = preflight(root, excluded_files=[str(broken)])
                    self.assertTrue(result.ok)
                    self.assertEqual(result.files_scanned, 1)
                    self.assertEqual(result.excluded_files, [str(broken.relative_to(root))])
                    metadata = _mission_audit_metadata(result)
                    self.assertEqual(json.loads(metadata["mission_excluded_files"]), result.excluded_files)
                    unexpected = missions / "new-error.json"
                    unexpected.write_text('{', encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "mission JSON read errors=1"):
                        preflight(root, excluded_files=[str(broken)])
                    unexpected.unlink()
                    # Valid but unsupported queries must still fail the audit.
                    valid.write_text(json.dumps({"query": '[out:json];node[unknown_tag]({{bbox}});out;'}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "missing"):
                        preflight(root, excluded_files=[str(broken)])
                    valid.write_text('{}', encoding="utf-8")
            self.assertEqual(broken.read_text(encoding="utf-8"), '{')

    def test_preflight_identifies_each_unreadable_mission_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            missions = root / "example-mission-pack" / "hpgmission"
            missions.mkdir(parents=True)
            malformed = missions / "broken.json"
            malformed.write_text('{"mission":\n}', encoding="utf-8")
            bad_encoding = missions / "encoding.json"
            bad_encoding.write_bytes(b'\xff')
            valid = missions / "valid.json"
            valid.write_text('{}', encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output):
                with self.assertRaisesRegex(ValueError, "mission JSON read errors=2"):
                    _run_mission_audit_preflight(root)

            log = output.getvalue()
            self.assertIn(f"JSON read error: {malformed}: JSONDecodeError:", log)
            self.assertIn("line 2 column 1", log)
            self.assertIn(f"JSON read error: {bad_encoding}: UnicodeDecodeError:", log)
            self.assertNotIn(str(valid), log)

    def test_failed_mission_audit_stops_before_geofabrik_fetch(self) -> None:
        args = SimpleNamespace(packages_root=r"C:\\MSFS\\Community")
        with (
            mock.patch(
                "local_osm.multibuild._run_mission_audit_preflight",
                side_effect=ValueError("H145 mission audit failed before download: missing any selector historic"),
            ),
            mock.patch("local_osm.multibuild.geofabrik.fetch_index") as fetch_index,
        ):
            with self.assertRaisesRegex(ValueError, "mission audit failed"):
                command_build(args)

        fetch_index.assert_not_called()

    def test_bundle_metadata_persists_mission_profile_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "bundle.sqlite3"
            source = root / "finland.osm.pbf"
            source.write_bytes(b"pbf")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.commit()
            con.close()

            audit = SimpleNamespace(
                profile_name="h145-missions-v1",
                mission_profile_hash="ec53b09c1caf0e56638ee2e45c4e7f7694987781203a844b84e96aff4101da50",
                packages=(
                    "68ponygt-actionpack-vehicles-offshore",
                    "hpg-airbus-h145",
                    "hpg-airbus-h145-ap",
                    "hpg-airbus-h145-z-andrew-mission-pack",
                ),
                packages_root=r"C:\\MSFS\\Community",
                files_scanned=43,
                files_with_osm=7,
                query_occurrences=148,
                unique_queries=95,
                selector_branches=208,
            )
            regions = [{
                "id": "europe/finland",
                "name": "Finland",
                "iso2": ["FI"],
                "pbf": "https://example.invalid/finland.osm.pbf",
            }]
            merge_stats = {
                "engine": "none",
                "replication_timestamp": "2026-08-20T00:00:00Z",
            }

            write_bundle_metadata(
                db_path,
                regions,
                [source],
                merge_stats,
                source,
                merged_pbf_retained=True,
                mission_audit=audit,
            )

            con = sqlite3.connect(db_path)
            rows = dict(con.execute("SELECT key,value FROM metadata"))
            con.close()

            self.assertEqual(rows["mission_profile_name"], "h145-missions-v1")
            self.assertEqual(rows["mission_profile_hash"], audit.mission_profile_hash)
            self.assertEqual(rows["mission_query_occurrences"], "148")
            self.assertEqual(rows["mission_unique_queries"], "95")
            self.assertEqual(rows["mission_selector_branches"], "208")
            self.assertEqual(rows["mission_files_scanned"], "43")
            self.assertEqual(rows["mission_files_with_osm"], "7")
            self.assertEqual(json.loads(rows["mission_packages"]), list(audit.packages))
            self.assertEqual(rows["mission_packages_root"], r"C:\\MSFS\\Community")


if __name__ == "__main__":
    unittest.main()
