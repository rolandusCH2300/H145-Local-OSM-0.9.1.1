"""Invoke PyInstaller without unrelated native libraries from the host PATH."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> None:
    # Set this inside Python: PyInstaller resolves dependent DLLs here and can
    # otherwise bundle e.g. a third-party ICU DLL in place of Windows' ICU.
    windows = Path(os.environ["SystemRoot"])
    os.environ["PATH"] = os.pathsep.join([
        sys.prefix,
        str(Path(sys.prefix) / "Scripts"),
        str(windows / "System32"),
        str(windows),
    ])
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONNOUSERSITE"] = "1"

    from PyInstaller.__main__ import run

    run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    main()
