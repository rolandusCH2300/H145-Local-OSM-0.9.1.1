import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from tools import build_portable_gui


class PortableBuildTests(unittest.TestCase):
    def test_packager_receives_clean_dll_search_path_and_original_arguments(self):
        pyinstaller = ModuleType("PyInstaller")
        entrypoint = ModuleType("PyInstaller.__main__")
        seen = {}

        def capture(args):
            seen.update(args=args, environment=dict(os.environ))

        entrypoint.run = Mock(side_effect=capture)
        windows = str(Path("system-root"))
        prefix = str(Path("isolated-python"))
        args = ["--clean", "--windowed", "--collect-all", "osmium", "app.py"]
        with (patch.dict(sys.modules, {"PyInstaller": pyinstaller, "PyInstaller.__main__": entrypoint}),
              patch.dict(os.environ, {"SystemRoot": windows, "PATH": "unrelated-native-libraries",
                                      "PYTHONPATH": "unrelated-python-modules"}),
              patch.object(sys, "prefix", prefix)):
            build_portable_gui.main(args)

        entrypoint.run.assert_called_once_with(args)
        self.assertEqual(seen["environment"]["PATH"].split(os.pathsep), [
            prefix, str(Path(prefix) / "Scripts"), str(Path(windows) / "System32"), windows,
        ])
        self.assertNotIn("PYTHONPATH", seen["environment"])
        self.assertEqual(seen["environment"]["PYTHONNOUSERSITE"], "1")


if __name__ == "__main__":
    unittest.main()
