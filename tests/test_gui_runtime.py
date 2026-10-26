from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from local_osm.gui import __main__ as gui_main
from local_osm.gui.probe import probe_local_server


class _Response:
    status = 200

    def __init__(self, payload: dict):
        self._stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, *args, **kwargs):
        return self._stream.read(*args, **kwargs)


class GuiRuntimeTests(unittest.TestCase):
    def test_packaged_build_forwards_each_mission_exclusion(self):
        exclusions = ["pack/hpgmission/one.json", "pack/hpgmission/two.json"]
        options = [value for path in exclusions for value in ("--exclude-mission-file", path)]
        with (
            mock.patch("local_osm.coverage.prepare_geofabrik_coverage"),
            mock.patch.object(gui_main, "_run_existing_main", return_value=1) as run,
        ):
            result = gui_main._internal_task([
                "build-api", "monaco", "--db", "test.sqlite3", *options,
            ])
        self.assertEqual(result, 1)
        forwarded = run.call_args.args[1]
        self.assertEqual(forwarded.count("--exclude-mission-file"), 2)
        for path in exclusions:
            self.assertIn(path, forwarded)
        with mock.patch.object(gui_main, "run_local_build") as run_local:
            gui_main._internal_task([
                "build-local", "local.osm.pbf", "--db", "test.sqlite3", *options,
            ])
        self.assertEqual(run_local.call_args.kwargs["excluded_mission_files"], exclusions)

    def test_gui_source_is_syntax_valid_without_importing_pyside(self):
        for path in (
            Path("local_osm/gui/app.py"),
            Path("local_osm/gui/enhanced_app.py"),
            Path("local_osm/gui/enhanced_app_v2.py"),
        ):
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")

    def test_frozen_entrypoint_calls_freeze_support_before_gui_task_import(self):
        path = Path("h145_local_osm_gui.py")
        source = path.read_text(encoding="utf-8")
        freeze_at = source.index("multiprocessing.freeze_support()")
        gui_import_at = source.index("from local_osm.gui.__main__ import main")
        self.assertLess(freeze_at, gui_import_at)

    def test_internal_descendants_are_blocked_before_gui_startup(self):
        path = Path("local_osm/gui/__main__.py")
        source = path.read_text(encoding="utf-8")
        marker = 'os.environ.get(_INTERNAL_PROCESS_ENV) == "1"'
        guard_at = source.index(marker)
        gui_import_at = source.index("from .enhanced_app_v2 import run_gui")
        self.assertLess(guard_at, gui_import_at)
        self.assertIn('os.environ[_INTERNAL_PROCESS_ENV] = "1"', source)
        self.assertIn("ProcessPoolExecutor", source)

    def test_internal_runner_reports_text_system_exit_as_normal_failure(self):
        stderr = io.StringIO()
        with mock.patch(
            "local_osm.multibuild.main",
            side_effect=SystemExit("build failed"),
        ):
            with redirect_stderr(stderr):
                result = gui_main._run_existing_main("multibuild", [])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue().strip(), "build failed")

    def test_packaged_benchmark_compare_routes_all_gate_arguments(self):
        with mock.patch.object(gui_main, "_run_existing_main", return_value=0) as run:
            result = gui_main._internal_task([
                "benchmark-compare",
                "--old", "old.sqlite3",
                "--new", "new.sqlite3",
                "--packages-root", "Community",
                "--repeat", "7",
                "--radius", "5000",
                "--lat", "60.1699",
                "--lon", "24.9384",
                "--max-regression-pct", "10",
                "--output-dir", "reports",
            ])

        self.assertEqual(result, 0)
        run.assert_called_once_with(
            "benchmark_compare",
            [
                "--old", "old.sqlite3",
                "--new", "new.sqlite3",
                "--packages-root", "Community",
                "--repeat", "7",
                "--radius", "5000",
                "--max-regression-pct", "10",
                "--output-dir", "reports",
                "--lat", "60.1699",
                "--lon", "24.9384",
            ],
        )

    @mock.patch("local_osm.gui.probe.urllib.request.urlopen")
    def test_probe_accepts_only_h145_local_osm_health(self, urlopen):
        urlopen.return_value = _Response(
            {"status": "ready", "service": "H145 Local OSM"}
        )
        self.assertTrue(probe_local_server())

        urlopen.return_value = _Response(
            {"status": "ready", "service": "Something Else"}
        )
        self.assertFalse(probe_local_server())

    @mock.patch("local_osm.gui.probe.urllib.request.urlopen", side_effect=OSError("offline"))
    def test_probe_failure_is_silent_false(self, _urlopen):
        self.assertFalse(probe_local_server())


if __name__ == "__main__":
    unittest.main()
