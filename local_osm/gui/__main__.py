from __future__ import annotations

import argparse
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

from ..network_errors import NETWORK_ERRORS, network_failure_message
from .merge_runtime import gui_merge_runtime
from .services import application_root, run_local_build
from .user_output import (
    suppress_expected_build_relation_notice,
    suppress_expected_validation_relation_warning,
)


_INTERNAL_PROCESS_ENV = "H145_LOCAL_OSM_INTERNAL_PROCESS"


def _gui_icon_path() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root) / "icon.png"
    return Path(__file__).resolve().parents[2] / "icon.png"


def _multiprocessing_probe_worker(value: int) -> int:
    return int(value) + 1


def _run_existing_main(module_name: str, argv: list[str]) -> int:
    if module_name == "validation":
        from .. import database_validation as module
    elif module_name == "benchmark":
        from .. import database_benchmark as module
    elif module_name == "benchmark_compare":
        from .. import database_benchmark_compare as module
    elif module_name == "multibuild":
        from .. import multibuild as module
    elif module_name == "equivalence":
        from .. import database_equivalence as module
    else:
        raise ValueError(module_name)

    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0], *argv]
        result = module.main()
        return int(result or 0)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(str(exc.code), file=sys.stderr)
        return 1
    finally:
        sys.argv = old_argv


def _staged_nodecache_path(database: Path) -> Path:
    """Return the sparse pyosmium cache path used by importer staging."""
    return Path(str(Path(database)) + ".building.nodecache")


def _schedule_frozen_nodecache_cleanup_after_exit(database: Path) -> bool:
    """Ensure frozen builds use the importer's deduplicated external cleanup."""
    cache_path = _staged_nodecache_path(database)
    if (
        os.name != "nt"
        or not getattr(sys, "frozen", False)
        or not cache_path.exists()
    ):
        return False

    from .._importer_v6_base import _schedule_windows_delete_after_exit
    return _schedule_windows_delete_after_exit(cache_path)


def _internal_task(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="h145-local-osm --internal-task")
    sub = parser.add_subparsers(dest="task", required=True)

    server = sub.add_parser("server")
    server.add_argument("--db", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("--db", required=True)
    validate.add_argument("--packages-root")
    validate.add_argument("--output-dir", required=True)

    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("--db", required=True)
    benchmark.add_argument("--packages-root")
    benchmark.add_argument("--repeat", default="7")
    benchmark.add_argument("--radius", default="5000")
    benchmark.add_argument("--lat")
    benchmark.add_argument("--lon")
    benchmark.add_argument("--output-dir", required=True)

    benchmark_compare = sub.add_parser("benchmark-compare")
    benchmark_compare.add_argument("--old", required=True)
    benchmark_compare.add_argument("--new", required=True)
    benchmark_compare.add_argument("--packages-root", required=True)
    benchmark_compare.add_argument("--repeat", default="7")
    benchmark_compare.add_argument("--radius", default="5000")
    benchmark_compare.add_argument("--lat")
    benchmark_compare.add_argument("--lon")
    benchmark_compare.add_argument("--max-regression-pct", default="10")
    benchmark_compare.add_argument("--output-dir", required=True)

    compare_db = sub.add_parser("compare-db")
    compare_db.add_argument("--old", required=True)
    compare_db.add_argument("--new", required=True)

    build_api = sub.add_parser("build-api")
    build_api.add_argument("regions", nargs="+")
    build_api.add_argument("--db", required=True)
    build_api.add_argument("--packages-root")
    build_api.add_argument("--workers", default="auto")
    build_api.add_argument("--replace", action="store_true")

    build_local = sub.add_parser("build-local")
    build_local.add_argument("pbfs", nargs="+")
    build_local.add_argument("--db", required=True)
    build_local.add_argument("--packages-root")
    build_local.add_argument("--workers", default="auto")
    for build_parser in (build_api, build_local):
        build_parser.add_argument("--exclude-mission-file", action="append", default=[])
    from ..bulk_write import add_build_write_arguments, build_write_cli_args
    add_build_write_arguments(build_api)
    add_build_write_arguments(build_local)

    sub.add_parser("multiprocessing-probe")

    args = parser.parse_args(argv)

    if args.task == "server":
        from ..server import serve

        serve(Path(args.db), port=8765, db_readers=1)
        return 0

    if args.task == "validate":
        command = ["--db", args.db, "--output-dir", args.output_dir]
        if args.packages_root:
            command.extend(["--packages-root", args.packages_root])
        with suppress_expected_validation_relation_warning():
            return _run_existing_main("validation", command)

    if args.task == "benchmark":
        command = [
            "--db",
            args.db,
            "--repeat",
            str(args.repeat),
            "--radius",
            str(args.radius),
            "--output-dir",
            args.output_dir,
        ]
        if (args.lat is None) != (args.lon is None):
            parser.error("benchmark --lat and --lon must be supplied together")
        if args.lat is not None:
            command.extend(["--lat", str(args.lat), "--lon", str(args.lon)])
        if args.packages_root:
            command.extend(["--packages-root", args.packages_root])
        return _run_existing_main("benchmark", command)

    if args.task == "benchmark-compare":
        command = [
            "--old", args.old,
            "--new", args.new,
            "--packages-root", args.packages_root,
            "--repeat", str(args.repeat),
            "--radius", str(args.radius),
            "--max-regression-pct", str(args.max_regression_pct),
            "--output-dir", args.output_dir,
        ]
        if (args.lat is None) != (args.lon is None):
            parser.error("benchmark-compare --lat and --lon must be supplied together")
        if args.lat is not None:
            command.extend(["--lat", str(args.lat), "--lon", str(args.lon)])
        return _run_existing_main("benchmark_compare", command)

    if args.task == "compare-db":
        return _run_existing_main(
            "equivalence",
            ["--old", args.old, "--new", args.new],
        )

    if args.task == "build-api":
        # The normal catalog intentionally uses Geofabrik's compact no-geometry
        # index. Fetch the geometry index once here, before the long PBF build,
        # and retain a compressed offline coverage catalog for the runtime guard.
        from ..coverage import install_coverage_metadata, prepare_geofabrik_coverage

        coverage_payload = prepare_geofabrik_coverage(args.regions)
        command = [
            "build",
            *args.regions,
            "--db",
            args.db,
            "--workers",
            args.workers,
            *build_write_cli_args(args),
        ]
        if args.replace:
            command.append("--replace")
        for path in args.exclude_mission_file:
            command.extend(["--exclude-mission-file", path])
        if args.packages_root:
            command.extend(["--packages-root", args.packages_root])
        with suppress_expected_build_relation_notice(), gui_merge_runtime():
            result = _run_existing_main("multibuild", command)
        if result == 0:
            install_coverage_metadata(Path(args.db), coverage_payload)
            _schedule_frozen_nodecache_cleanup_after_exit(Path(args.db))
        return result

    if args.task == "build-local":
        with suppress_expected_build_relation_notice(), gui_merge_runtime():
            run_local_build(
                [Path(path) for path in args.pbfs],
                Path(args.db),
                community=Path(args.packages_root) if args.packages_root else None,
                excluded_mission_files=args.exclude_mission_file,
                workers=args.workers,
                sql_batch_rows=args.sql_batch_rows,
                writer_batch_size=args.writer_batch_size,
                sql_write_mode=args.sql_write_mode,
                root=application_root(),
            )
        _schedule_frozen_nodecache_cleanup_after_exit(Path(args.db))
        return 0

    if args.task == "multiprocessing-probe":
        import concurrent.futures as futures

        spawn_ctx = multiprocessing.get_context("spawn")
        with futures.ProcessPoolExecutor(max_workers=1, mp_context=spawn_ctx) as executor:
            result = executor.submit(_multiprocessing_probe_worker, 41).result(timeout=20)
        if result != 42:
            raise RuntimeError(f"multiprocessing probe returned {result!r}, expected 42")
        print("Packaged multiprocessing spawn probe passed")
        return 0

    raise AssertionError(args.task)


def _line_buffer_internal_output() -> None:
    """Make long-running child-task progress visible to the Qt parent promptly."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True, write_through=True)
        except (OSError, ValueError):
            pass


def main() -> int:
    multiprocessing.freeze_support()
    argv = sys.argv[1:]

    if argv and argv[0] == "--internal-task":
        # Mark the build/server/diagnostic process before it can create any
        # multiprocessing descendants. The marker is inherited by spawned
        # ProcessPoolExecutor workers and resource-tracker processes.
        os.environ[_INTERNAL_PROCESS_ENV] = "1"
        _line_buffer_internal_output()
        try:
            return _internal_task(argv[1:])
        except NETWORK_ERRORS as exc:
            print(
                network_failure_message(exc),
                file=sys.stderr,
                flush=True,
            )
            return 1

    if os.environ.get(_INTERNAL_PROCESS_ENV) == "1":
        # A frozen descendant that was not consumed by PyInstaller's
        # freeze_support() must never fall through into normal GUI startup.
        # This is intentionally fail-closed: internal descendants may exit,
        # but they can never create a second H145 Local OSM window.
        return 0

    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication

        from .enhanced_app_v2 import run_gui
    except ImportError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            raise SystemExit(
                "The GUI requires PySide6. Install requirements-release.txt or use the packaged app."
            ) from exc
        raise

    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = _gui_icon_path()
    if icon_path.is_file():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            app.setWindowIcon(icon)

    return run_gui()
