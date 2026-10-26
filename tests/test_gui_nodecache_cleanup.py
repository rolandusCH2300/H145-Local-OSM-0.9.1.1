from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_osm.gui import __main__ as gui_main


class GuiNodecacheCleanupTests(unittest.TestCase):
    def test_staged_nodecache_path_matches_importer_staging_name(self) -> None:
        database = Path("data") / "finland.sqlite3"
        self.assertEqual(
            gui_main._staged_nodecache_path(database),
            Path("data") / "finland.sqlite3.building.nodecache",
        )

    def test_frozen_windows_cleanup_uses_external_powershell_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "test.sqlite3.building.nodecache"
            cache_path.write_bytes(b"cache")

            with (
                patch.object(gui_main, "_staged_nodecache_path", return_value=cache_path),
                patch.object(gui_main.os, "name", "nt"),
                patch.object(gui_main.os, "getpid", return_value=4321),
                patch.object(gui_main.sys, "frozen", True, create=True),
                patch.object(gui_main.subprocess, "Popen") as popen,
            ):
                scheduled = gui_main._schedule_frozen_nodecache_cleanup_after_exit(
                    Path(tmp) / "test.sqlite3"
                )

            self.assertTrue(scheduled)
            command = popen.call_args.args[0]
            self.assertEqual(command[0], "powershell.exe")
            self.assertIn("Wait-Process -Id $parentPid", command[-1])
            self.assertIn("$parentPid=4321", command[-1])
            # Windows runners may expose TEMP through an 8.3 path alias.
            self.assertIn(str(cache_path.resolve()).replace("'", "''"), command[-1])


if __name__ == "__main__":
    unittest.main()
