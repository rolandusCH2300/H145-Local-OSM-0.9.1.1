import tempfile
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from local_osm import _importer_v6_base as importer
from local_osm import multibuild
from local_osm.gui import __main__ as gui


class BuildWriteControlTests(unittest.TestCase):
    def test_local_internal_task_forwards_independent_controls(self):
        with (patch.object(gui, "run_local_build") as build,
              patch.object(gui, "gui_merge_runtime", return_value=nullcontext()),
              patch.object(gui, "_schedule_frozen_nodecache_cleanup_after_exit")):
            result = gui._internal_task([
                "build-local", "input.osm.pbf", "--db", "output.sqlite3",
                "--sql-batch-rows", "128", "--writer-batch-size", "16000",
                "--sql-write-mode", "direct",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(build.call_args.kwargs["sql_batch_rows"], 128)
        self.assertEqual(build.call_args.kwargs["writer_batch_size"], 16000)
        self.assertEqual(build.call_args.kwargs["sql_write_mode"], "direct")

    def test_api_internal_task_forwards_independent_controls(self):
        with (patch("local_osm.coverage.prepare_geofabrik_coverage", return_value=b"coverage"),
              patch("local_osm.coverage.install_coverage_metadata"),
              patch.object(gui, "_run_existing_main", return_value=0) as run,
              patch.object(gui, "gui_merge_runtime", return_value=nullcontext()),
              patch.object(gui, "_schedule_frozen_nodecache_cleanup_after_exit")):
            result = gui._internal_task([
                "build-api", "monaco", "--db", "output.sqlite3", "--replace",
                "--sql-batch-rows", "128", "--writer-batch-size", "16000",
                "--sql-write-mode", "direct",
            ])
        self.assertEqual(result, 0)
        module, command = run.call_args.args
        self.assertEqual(module, "multibuild")
        self.assertEqual(command[command.index("--sql-batch-rows") + 1], "128")
        self.assertEqual(command[command.index("--writer-batch-size") + 1], "16000")
        self.assertEqual(command[command.index("--sql-write-mode") + 1], "direct")

    def test_local_internal_task_preserves_grouped_default(self):
        with (patch.object(gui, "run_local_build") as build,
              patch.object(gui, "gui_merge_runtime", return_value=nullcontext()),
              patch.object(gui, "_schedule_frozen_nodecache_cleanup_after_exit")):
            gui._internal_task(["build-local", "input.osm.pbf", "--db", "output.sqlite3"])
        self.assertEqual(build.call_args.kwargs["sql_write_mode"], "grouped")
        self.assertEqual(build.call_args.kwargs["sql_batch_rows"], 64)
        self.assertEqual(build.call_args.kwargs["writer_batch_size"], 64_000)
        self.assertEqual(build.call_args.kwargs["workers"], "auto")

    def test_importer_cli_uses_tested_defaults_and_allows_serial_override(self):
        for extra, expected_workers in (([], "auto"), (["--workers", "0"], "0")):
            with (
                self.subTest(extra=extra),
                patch.object(sys, "argv", ["importer", "--pbf", "input.osm.pbf",
                                           "--db", "output.sqlite3", *extra]),
                patch.object(importer, "import_pbf") as build,
            ):
                importer.main()
            self.assertEqual(build.call_args.kwargs["workers"], expected_workers)
            self.assertEqual(build.call_args.kwargs["worker_batch_size"], 4_000)
            self.assertEqual(build.call_args.kwargs["sql_write_mode"], "grouped")
            self.assertEqual(build.call_args.kwargs["sql_batch_rows"], 64)
            self.assertEqual(build.call_args.kwargs["writer_batch_size"], 64_000)

    def test_multibuild_cli_uses_tested_defaults_and_allows_serial_override(self):
        for extra, expected_workers in (([], "auto"), (["--workers", "0"], "0")):
            with (
                self.subTest(extra=extra),
                patch.object(sys, "argv", ["multibuild", "build", "monaco",
                                           "--db", "output.sqlite3", *extra]),
                patch.object(multibuild, "command_build", return_value=0) as build,
                self.assertRaises(SystemExit) as exited,
            ):
                multibuild.main()
            self.assertEqual(exited.exception.code, 0)
            args = build.call_args.args[0]
            self.assertEqual(args.workers, expected_workers)
            self.assertEqual(args.worker_batch_size, 4_000)
            self.assertEqual(args.sql_write_mode, "grouped")
            self.assertEqual(args.sql_batch_rows, 64)
            self.assertEqual(args.writer_batch_size, 64_000)

    def test_api_internal_task_uses_tested_defaults(self):
        with (patch("local_osm.coverage.prepare_geofabrik_coverage", return_value=b"coverage"),
              patch("local_osm.coverage.install_coverage_metadata"),
              patch.object(gui, "_run_existing_main", return_value=0) as run,
              patch.object(gui, "gui_merge_runtime", return_value=nullcontext()),
              patch.object(gui, "_schedule_frozen_nodecache_cleanup_after_exit")):
            gui._internal_task(["build-api", "monaco", "--db", "output.sqlite3"])
        module, command = run.call_args.args
        self.assertEqual(module, "multibuild")
        for flag, expected in (("--workers", "auto"), ("--sql-write-mode", "grouped"),
                               ("--sql-batch-rows", "64"), ("--writer-batch-size", "64000")):
            self.assertEqual(command[command.index(flag) + 1], expected)

    def test_auto_workers_match_benchmark_and_scale_down_on_smaller_hosts(self):
        for logical, expected in ((None, 1), (4, 1), (8, 3), (20, 8), (64, 8)):
            with self.subTest(logical=logical), patch.object(importer.os, "cpu_count", return_value=logical):
                self.assertEqual(importer._resolve_worker_count("auto"), expected)

    def test_locked_node_cache_schedules_cleanup_without_retry_delay(self):
        path = Mock()
        locked = PermissionError("file is mapped")
        locked.winerror = 32
        path.unlink.side_effect = locked
        with (patch.object(importer, "_schedule_windows_delete_after_exit", return_value=True) as schedule,
              patch.object(importer, "_unlink_with_retries") as retry):
            importer._cleanup_node_cache(path)
        schedule.assert_called_once_with(path)
        retry.assert_not_called()

    def test_cleanup_helper_failure_keeps_retry_fallback(self):
        path = Mock()
        locked = PermissionError("file is mapped")
        locked.winerror = 32
        path.unlink.side_effect = locked
        with (patch.object(importer, "_schedule_windows_delete_after_exit", return_value=False),
              patch.object(importer, "_unlink_with_retries", return_value=True) as retry):
            importer._cleanup_node_cache(path)
        retry.assert_called_once_with(path)

    def test_external_cleanup_is_hidden_quoted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "it's cache.nodecache"
            cache.write_bytes(b"cache")
            with (patch.object(importer.os, "name", "nt"),
                  patch.object(importer.subprocess, "Popen") as popen):
                self.assertTrue(importer._schedule_windows_delete_after_exit(cache))
                self.assertTrue(importer._schedule_windows_delete_after_exit(cache))
            popen.assert_called_once()
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "powershell.exe")
            self.assertIn("Hidden", command)
            self.assertEqual(popen.call_args.kwargs["creationflags"] & getattr(importer.subprocess, "DETACHED_PROCESS", 0), 0)
            self.assertIn("it''s cache.nodecache", command[-1])
            self.assertIn("Remove-Item -LiteralPath $cachePath", command[-1])


if __name__ == "__main__":
    unittest.main()
