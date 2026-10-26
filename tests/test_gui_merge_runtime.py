from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from local_osm import multibuild
from local_osm.gui import merge_runtime


class GuiMergeRuntimeTests(unittest.TestCase):
    def test_osmium_subprocess_uses_create_no_window_on_windows(self) -> None:
        with (
            mock.patch.object(merge_runtime.sys, "platform", "win32"),
            mock.patch.object(merge_runtime.subprocess, "run") as original_run,
        ):
            with merge_runtime.suppress_osmium_console_window():
                merge_runtime.subprocess.run(
                    [r"C:\\app\\tools\\osmium\\osmium.exe", "merge"],
                    check=True,
                )

        kwargs = original_run.call_args.kwargs
        expected = int(getattr(merge_runtime.subprocess, "CREATE_NO_WINDOW", 0x08000000))
        self.assertEqual(int(kwargs["creationflags"]) & expected, expected)

    def test_non_osmium_subprocess_is_not_modified(self) -> None:
        with (
            mock.patch.object(merge_runtime.sys, "platform", "win32"),
            mock.patch.object(merge_runtime.subprocess, "run") as original_run,
        ):
            with merge_runtime.suppress_osmium_console_window():
                merge_runtime.subprocess.run(["other-tool.exe"], check=True)

        self.assertNotIn("creationflags", original_run.call_args.kwargs)

    def test_gui_runtime_announces_osmium_merge_in_task_output(self) -> None:
        output = io.StringIO()
        result = {"engine": "osmium"}
        with mock.patch.object(
            multibuild,
            "_merge_with_osmium_cli",
            return_value=result,
        ) as original_merge:
            with redirect_stdout(output):
                with merge_runtime.gui_merge_runtime():
                    actual = multibuild._merge_with_osmium_cli(
                        [Path("one.osm.pbf"), Path("two.osm.pbf")],
                        Path("merged.osm.pbf"),
                        "osmium.exe",
                    )

        self.assertIs(actual, result)
        original_merge.assert_called_once()
        self.assertIn("Merging selected .osm.pbf files with Osmium Tool", output.getvalue())

    def test_gui_runtime_warns_clearly_for_python_fallback(self) -> None:
        output = io.StringIO()
        result = {"engine": "python"}
        with mock.patch.object(
            multibuild,
            "_merge_with_python",
            return_value=result,
        ) as original_merge:
            with redirect_stdout(output):
                with merge_runtime.gui_merge_runtime():
                    actual = multibuild._merge_with_python(
                        object(),
                        [Path("one.osm.pbf"), Path("two.osm.pbf")],
                        Path("merged.osm.pbf"),
                    )

        self.assertIs(actual, result)
        original_merge.assert_called_once()
        text = output.getvalue()
        self.assertIn("WARNING", text)
        self.assertIn("very long time", text)
        self.assertIn("Cancelling this build is recommended", text)
        self.assertIn("bug report", text)
        self.assertIn("single .osm.pbf", text)


if __name__ == "__main__":
    unittest.main()
