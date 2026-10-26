from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from local_osm.gui.services import (
    LEGACY_HPG_BACKUP_SUFFIX,
    LOCAL_OVERPASS_URL,
    OFFICIAL_OVERPASS_URL,
    PortableConfigStore,
    _community_from_usercfg,
    api_build_command,
    choose_default_database,
    configure_hpg_connection,
    discover_databases,
    discover_missions,
    hpg_connection_status,
    local_build_command,
    local_database_name,
    restore_hpg_connection,
    run_local_build,
)


class PortableConfigTests(unittest.TestCase):
    def test_config_is_stored_in_application_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = PortableConfigStore(root)
            self.assertEqual(store.load(), {})
            store.update(last_database="finland.sqlite3", community_folder="C:/MSFS/Community")
            self.assertEqual(store.path, root.resolve() / "config.json")
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["last_database"], "finland.sqlite3")
            self.assertEqual(store.load()["community_folder"], "C:/MSFS/Community")

    def test_invalid_config_fails_closed_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("not json", encoding="utf-8")
            self.assertEqual(PortableConfigStore(root).load(), {})


class DatabaseSelectionTests(unittest.TestCase):
    def test_last_database_wins_otherwise_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            old = data / "old.sqlite3"
            new = data / "new.sqlite3"
            old.write_bytes(b"old")
            time.sleep(0.002)
            new.write_bytes(b"new")
            databases = discover_databases(root)
            self.assertEqual(databases[0].name, "new.sqlite3")
            self.assertEqual(choose_default_database(databases, "old.sqlite3"), old.resolve())
            self.assertEqual(choose_default_database(databases, None), new.resolve())


class CommunityAndMissionTests(unittest.TestCase):
    def test_usercfg_installed_packages_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packages = root / "Packages"
            community = packages / "Community"
            community.mkdir(parents=True)
            cfg = root / "UserCfg.opt"
            cfg.write_text(f'InstalledPackagesPath "{packages}"\n', encoding="utf-8")
            self.assertEqual(_community_from_usercfg(cfg), community.resolve())

    def test_discovers_all_json_inside_hpgmission_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            good = community / "pack-a" / "hpgmission" / "Missions" / "one.json"
            other = community / "pack-b" / "hpgmission" / "two.json"
            ignored = community / "pack-a" / "config" / "not-a-mission.json"
            for path in (good, other, ignored):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
            entries = discover_missions(community)
            self.assertEqual([entry.package for entry in entries], ["pack-a", "pack-b"])
            self.assertTrue(all("hpgmission" in entry.relative_path.lower() for entry in entries))


class HPGConnectionTests(unittest.TestCase):
    def _write_mission_system(self, community: Path, url: str) -> Path:
        path = community / "hpg-airbus-h145" / "Config" / "MissionSystem.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "OverpassApiUrls": [
                        {"name": "Overpass DE", "url": url},
                        {"name": "Kumi", "url": "https://example.invalid"},
                    ]
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def test_configure_and_restore_preserve_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            mission = self._write_mission_system(community, OFFICIAL_OVERPASS_URL)
            status = configure_hpg_connection(community)
            self.assertTrue(status.configured)
            self.assertEqual(status.current_url, LOCAL_OVERPASS_URL)
            backup = Path(str(mission) + LEGACY_HPG_BACKUP_SUFFIX)
            self.assertTrue(backup.is_file())
            restored = restore_hpg_connection(community)
            self.assertFalse(restored.configured)
            self.assertEqual(restored.current_url, OFFICIAL_OVERPASS_URL)

    def test_nonstandard_endpoint_requires_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            community = Path(tmp)
            self._write_mission_system(community, "https://custom.invalid/api/interpreter?data=")
            with self.assertRaisesRegex(ValueError, "non-standard URL"):
                configure_hpg_connection(community)
            status = configure_hpg_connection(community, allow_nonstandard=True)
            self.assertTrue(status.configured)

    def test_missing_mission_system_is_status_not_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = hpg_connection_status(Path(tmp))
            self.assertFalse(status.configured)
            self.assertIsNone(status.mission_system)


class CommandTests(unittest.TestCase):
    @mock.patch("local_osm.gui.services.sys.executable", "python-test")
    def test_gui_defaults_to_parallel_and_limited_hardware_uses_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "data.sqlite3"
            community = Path(tmp) / "Community"
            community.mkdir()
            with mock.patch("local_osm.gui.services.sys.frozen", False, create=True):
                normal = api_build_command(["finland"], db, community, serial_topology=False)
                limited = api_build_command(["finland"], db, community, serial_topology=True)
                local = local_build_command([Path(tmp) / "x.osm.pbf"], db, community, serial_topology=True)
            self.assertIn("auto", normal)
            self.assertIn("0", limited)
            self.assertIn("0", local)
            self.assertIn("--packages-root", normal)

    def test_local_database_name_is_portable_and_stable(self):
        paths = [Path("Finland-latest.osm.pbf"), Path("Norway-latest.osm.pbf")]
        self.assertEqual(local_database_name(paths), "finland-norway.sqlite3")


class LocalBuildProgressTests(unittest.TestCase):
    def test_multiple_local_pbfs_report_merge_then_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "finland.osm.pbf"
            second = root / "norway.osm.pbf"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            database = root / "data" / "finland-norway.sqlite3"

            def fake_merge(
                paths,
                output,
                *,
                engine="auto",
                require_snapshot_consistency=True,
                **_kwargs,
            ):
                self.assertEqual(list(paths), [first.resolve(), second.resolve()])
                self.assertEqual(engine, "auto")
                self.assertFalse(require_snapshot_consistency)
                Path(output).write_bytes(b"merged")
                return {
                    "engine": "osmium",
                    "source_count": 2,
                    "same_replication_snapshot": True,
                }

            def fake_import(_source, db_path, **_kwargs):
                con = sqlite3.connect(db_path)
                try:
                    con.execute(
                        "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)"
                    )
                    con.commit()
                finally:
                    con.close()

            output = io.StringIO()
            with (
                mock.patch("local_osm.gui.services.merge_pbfs", side_effect=fake_merge),
                mock.patch("local_osm.gui.services.import_pbf", side_effect=fake_import),
                redirect_stdout(output),
            ):
                run_local_build(
                    [first, second],
                    database,
                    root=root,
                )

            text = output.getvalue()
            markers = [
                "[merge] Combining 2 local PBF files...",
                "[merge] Output:",
                "[merge] Engine: osmium",
                "[merge] Complete",
                "[import] Starting database import:",
                "Local PBF build complete:",
            ]
            positions = [text.index(marker) for marker in markers]
            self.assertEqual(positions, sorted(positions))
            self.assertFalse(
                (root / "downloads" / ".localbuild" / "finland-norway.merged.osm.pbf").exists()
            )


if __name__ == "__main__":
    unittest.main()
