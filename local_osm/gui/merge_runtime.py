from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_OSMIUM_NAMES = {"osmium", "osmium.exe"}


def _command_executable(args, kwargs) -> str:
    command = kwargs.get("args")
    if command is None and args:
        command = args[0]
    if isinstance(command, (list, tuple)) and command:
        return str(command[0])
    if isinstance(command, (str, Path)):
        return str(command)
    return ""


def _is_osmium_command(args, kwargs) -> bool:
    executable = _command_executable(args, kwargs)
    return Path(executable).name.lower() in _OSMIUM_NAMES if executable else False


@contextmanager
def suppress_osmium_console_window() -> Iterator[None]:
    """Prevent osmium-tool from creating a separate Windows console window.

    The GUI launches build tasks with stdout/stderr connected to its build log.
    A console-subsystem osmium.exe would otherwise create a visible CMD window
    when the parent executable is windowed. CREATE_NO_WINDOW keeps the child in
    the same redirected task-output pipeline without changing its exit status.
    """
    if sys.platform != "win32":
        yield
        return

    original_run = subprocess.run
    no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def run_without_osmium_console(*args, **kwargs):
        if _is_osmium_command(args, kwargs):
            existing = int(kwargs.get("creationflags", 0) or 0)
            kwargs["creationflags"] = existing | no_window
        return original_run(*args, **kwargs)

    subprocess.run = run_without_osmium_console
    try:
        yield
    finally:
        subprocess.run = original_run


def _print_python_fallback_warning() -> None:
    print("[merge] WARNING: Osmium Tool was not found; using the Python merge fallback.")
    print(
        "[merge] WARNING: The merge will complete, but it can take a very long time. "
        "Cancelling this build is recommended."
    )
    print(
        "[merge] WARNING: Please send a bug report so the missing Osmium Tool can be "
        "investigated, or build from a single .osm.pbf file instead."
    )


@contextmanager
def gui_merge_runtime() -> Iterator[None]:
    """Adapt the shared PBF merger for GUI builds without changing CLI behavior."""
    from .. import multibuild

    original_osmium_merge = multibuild._merge_with_osmium_cli
    original_python_merge = multibuild._merge_with_python

    def osmium_merge(paths, output, executable):
        print("[merge] Merging selected .osm.pbf files with Osmium Tool...")
        return original_osmium_merge(paths, output, executable)

    def python_merge(osmium, paths, output):
        _print_python_fallback_warning()
        return original_python_merge(osmium, paths, output)

    multibuild._merge_with_osmium_cli = osmium_merge
    multibuild._merge_with_python = python_merge
    try:
        with suppress_osmium_console_window():
            yield
    finally:
        multibuild._merge_with_osmium_cli = original_osmium_merge
        multibuild._merge_with_python = original_python_merge
