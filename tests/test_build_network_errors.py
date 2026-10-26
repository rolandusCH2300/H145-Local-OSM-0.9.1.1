import contextlib
import io
import unittest
import urllib.error
from unittest.mock import patch

from local_osm import coverage, geofabrik, multibuild
from local_osm.gui import __main__ as entry
from local_osm.network_errors import NETWORK_ERROR_PREFIX


class BuildNetworkErrorTests(unittest.TestCase):
    def test_network_failures_exit_cleanly_with_recovery_advice(self):
        for error in (
            urllib.error.URLError(TimeoutError("SSL handshake timed out")),
            TimeoutError("read timed out"),
            ConnectionResetError("connection reset"),
            urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, None),
        ):
            with self.subTest(error=error), patch.object(
                entry.sys, "argv", ["app", "--internal-task", "build-api", "finland"]
            ), patch.dict(entry.os.environ), patch.object(
                entry, "_line_buffer_internal_output"
            ), patch.object(entry, "_internal_task", side_effect=error):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    self.assertEqual(entry.main(), 1)
                self.assertIn("Network request failed", output.getvalue())
                self.assertIn("Local .osm.pbf files", output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())

    def test_all_api_build_network_stages_report_the_gui_error_marker(self):
        region = {"id": "finland", "name": "Finland", "iso2": ["FI"],
                  "pbf": "https://example.invalid/finland.osm.pbf"}
        for stage in ("coverage", "catalogue", "preflight", "download"):
            with self.subTest(stage=stage), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(entry.sys, "argv", [
                    "app", "--internal-task", "build-api", "finland", "--db", "output.sqlite3",
                ]))
                stack.enter_context(patch.dict(entry.os.environ))
                stack.enter_context(patch.object(entry, "_line_buffer_internal_output"))
                stack.enter_context(patch.object(entry, "gui_merge_runtime", return_value=contextlib.nullcontext()))
                stages = {
                    "coverage": stack.enter_context(patch.object(coverage, "prepare_geofabrik_coverage", return_value="coverage")),
                    "catalogue": stack.enter_context(patch.object(geofabrik, "fetch_index", return_value={})),
                    "preflight": stack.enter_context(patch.object(multibuild, "preflight_regions", return_value=([{"size": 1, "md5": "hash"}], "timestamp"))),
                    "download": stack.enter_context(patch.object(multibuild, "ensure_region_download")),
                }
                stack.enter_context(patch.object(multibuild, "_run_mission_audit_preflight", return_value=None))
                stack.enter_context(patch.object(multibuild, "resolve_regions", return_value=[region]))
                build = stack.enter_context(patch.object(multibuild, "import_pbf"))
                install = stack.enter_context(patch.object(coverage, "install_coverage_metadata"))
                stages[stage].side_effect = TimeoutError(f"{stage} timed out")
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(entry.main(), 1)
                self.assertIn(NETWORK_ERROR_PREFIX, stderr.getvalue())
                self.assertIn(f"{stage} timed out", stderr.getvalue())
                self.assertIn("Local .osm.pbf files", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                build.assert_not_called()
                install.assert_not_called()

    def test_metadata_progress_is_visible_before_the_blocking_request(self):
        for operation, expected in ((geofabrik.fetch_index, "region catalogue"),
                                    (lambda: coverage.prepare_geofabrik_coverage(["finland"]), "coverage boundaries")):
            with self.subTest(expected=expected):
                output = io.StringIO()

                def timeout(_request, *, timeout):
                    self.assertEqual(timeout, 30)
                    self.assertIn(expected, output.getvalue())
                    raise TimeoutError("simulated timeout")

                with (patch("urllib.request.urlopen", side_effect=timeout),
                      contextlib.redirect_stdout(output), self.assertRaises(TimeoutError)):
                    operation()

    def test_local_file_errors_are_not_misreported_as_api_failures(self):
        with (patch.object(entry.sys, "argv", ["multibuild", "build", "finland"]),
              patch.object(multibuild, "command_build", side_effect=PermissionError("database is locked")),
              self.assertRaises(SystemExit) as exited):
            multibuild.main()
        self.assertIn("database is locked", str(exited.exception))
        self.assertNotIn(NETWORK_ERROR_PREFIX, str(exited.exception))

    def test_programming_errors_are_not_hidden_as_network_failures(self):
        with patch.object(
            entry.sys, "argv", ["app", "--internal-task", "build-api", "finland"]
        ), patch.dict(entry.os.environ), patch.object(
            entry, "_line_buffer_internal_output"
        ), patch.object(entry, "_internal_task", side_effect=ValueError("invalid data")):
            with self.assertRaisesRegex(ValueError, "invalid data"):
                entry.main()
