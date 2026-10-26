from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hydration_cache import inspect_hydration_cache
from .mission_audit import audit_occurrences, scan_mission_queries
from .planner_stats import inspect_planner_stats
from .road_index import inspect_road_index
from .tag_spatial_index import inspect_tag_spatial_index


REQUIRED_CORE_TABLES = (
    "objects",
    "object_rtree",
    "object_tags",
    "node_coords",
    "area_geometries",
    "way_geometry_v5",
    "relation_members",
    "metadata",
    "planner_key_stats",
    "planner_value_stats",
)


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    name: str
    ok: bool
    severity: str = "error"  # error or warning
    actual: Any = None
    expected: Any = None
    detail: str = ""


@dataclass(slots=True)
class ValidationReport:
    database: str
    generated_at: str
    file_size_bytes: int
    quick_check: str
    metadata: dict[str, str]
    counts: dict[str, int]
    derived: dict[str, Any]
    mission: dict[str, Any] | None = None
    checks: list[ValidationCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any((not check.ok) and check.severity == "error" for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if (not check.ok) and check.severity == "warning")


def _ro_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def _metadata(con: sqlite3.Connection) -> dict[str, str]:
    try:
        return {str(row[0]): str(row[1]) for row in con.execute("SELECT key,value FROM metadata")}
    except sqlite3.Error:
        return {}


def _tables(con: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(con: sqlite3.Connection, table: str) -> int:
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _selected_iso2(metadata: dict[str, str]) -> tuple[str, ...]:
    raw = metadata.get("source_regions")
    if not raw:
        return ()
    try:
        regions = json.loads(raw)
    except Exception:
        return ()
    codes = {
        str(code).strip().upper()
        for region in regions if isinstance(region, dict)
        for code in (region.get("iso2") or [])
        if str(code).strip()
    }
    return tuple(sorted(codes))


def _add(
    checks: list[ValidationCheck],
    name: str,
    ok: bool,
    *,
    actual: Any = None,
    expected: Any = None,
    detail: str = "",
    severity: str = "error",
) -> None:
    checks.append(ValidationCheck(name, bool(ok), severity, actual, expected, detail))


def validate_database(
    db_path: str | Path,
    *,
    packages_root: str | Path | None = None,
    require_derived: bool = True,
) -> ValidationReport:
    """Read-only acceptance check for one explicitly selected built database."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    checks: list[ValidationCheck] = []
    con = _ro_connection(path)
    try:
        quick_row = con.execute("PRAGMA quick_check").fetchone()
        quick = str(quick_row[0]) if quick_row is not None else "no result"
        _add(checks, "sqlite_quick_check", quick.lower() == "ok", actual=quick, expected="ok")

        tables = _tables(con)
        missing_tables = [name for name in REQUIRED_CORE_TABLES if name not in tables]
        _add(
            checks,
            "required_core_tables",
            not missing_tables,
            actual=missing_tables,
            expected=[],
            detail="Required Schema-5 tables must be present.",
        )

        metadata = _metadata(con)
        try:
            schema_version = int(metadata.get("schema_version", "0"))
        except ValueError:
            schema_version = 0
        _add(checks, "schema_version", schema_version >= 5, actual=schema_version, expected=">=5")
        _add(
            checks,
            "profile",
            bool(metadata.get("profile")),
            actual=metadata.get("profile"),
            expected="non-empty import profile",
        )

        counts: dict[str, int] = {}
        for name in (
            "objects",
            "object_rtree",
            "object_tags",
            "node_coords",
            "area_geometries",
            "way_geometry_v5",
            "relation_members",
        ):
            counts[name] = _count(con, name) if name in tables else 0
        _add(checks, "objects_nonempty", counts["objects"] > 0, actual=counts["objects"], expected=">0")
        _add(checks, "object_tags_nonempty", counts["object_tags"] > 0, actual=counts["object_tags"], expected=">0")
        _add(
            checks,
            "object_rtree_row_count",
            counts["object_rtree"] == counts["objects"],
            actual=counts["object_rtree"],
            expected=counts["objects"],
            detail="Every retained object must have exactly one global RTree entry.",
        )
        _add(
            checks,
            "schema5_way_geometry_nonempty",
            counts["way_geometry_v5"] > 0,
            actual=counts["way_geometry_v5"],
            expected=">0",
            severity="error" if require_derived else "warning",
        )

        selected_iso2 = _selected_iso2(metadata)
        if selected_iso2:
            _add(
                checks,
                "selected_country_geometries",
                counts["area_geometries"] >= len(selected_iso2),
                actual=counts["area_geometries"],
                expected=f">={len(selected_iso2)} for {','.join(selected_iso2)}",
            )

        import_stats: dict[str, Any] = {}
        if metadata.get("import_stats"):
            try:
                parsed = json.loads(metadata["import_stats"])
                if isinstance(parsed, dict):
                    import_stats = parsed
            except Exception:
                _add(checks, "import_stats_json", False, actual="invalid JSON", expected="valid JSON")
        if import_stats:
            _add(
                checks,
                "missing_way_geometry",
                int(import_stats.get("missing_way_geometry", 0)) == 0,
                actual=int(import_stats.get("missing_way_geometry", 0)),
                expected=0,
            )
            expected_rows = int(import_stats.get("way_geometry_rows", counts["way_geometry_v5"]))
            _add(
                checks,
                "way_geometry_row_count",
                counts["way_geometry_v5"] == expected_rows,
                actual=counts["way_geometry_v5"],
                expected=expected_rows,
            )
            unresolved = int(import_stats.get("unresolved_relations", 0))
            _add(
                checks,
                "unresolved_relations",
                unresolved == 0,
                actual=unresolved,
                expected=0,
                severity="warning",
                detail="Non-zero unresolved relations do not necessarily make mission queries incorrect.",
            )
            missing_deps = int(import_stats.get("missing_relation_dependencies", 0))
            _add(
                checks,
                "missing_relation_dependencies",
                missing_deps == 0,
                actual=missing_deps,
                expected=0,
                severity="warning",
                detail="Geofabrik extracts can legitimately omit referenced relations outside the extract.",
            )
            stats_country = int(import_stats.get("country_geometries", counts["area_geometries"]))
            _add(
                checks,
                "country_geometry_stat_consistency",
                counts["area_geometries"] == stats_country,
                actual=counts["area_geometries"],
                expected=stats_country,
                severity="warning",
            )

        road = inspect_road_index(con)
        planner = inspect_planner_stats(con)
        tag_spatial = inspect_tag_spatial_index(con)
        hydration = inspect_hydration_cache(con)
        derived = {
            "road_index": road,
            "planner_stats": planner,
            "tag_spatial_index": tag_spatial,
            "hydration_cache": hydration,
        }
        severity = "error" if require_derived else "warning"
        _add(checks, "road_index_ready", bool(road["ready"]), actual=bool(road["ready"]), expected=True, severity=severity)
        _add(
            checks,
            "road_index_fast_nodes_ready",
            bool(road["fast_nodes_ready"]),
            actual=bool(road["fast_nodes_ready"]),
            expected=True,
            severity=severity,
        )
        _add(checks, "planner_stats_ready", bool(planner["ready"]), actual=bool(planner["ready"]), expected=True, severity=severity)
        _add(
            checks,
            "tag_spatial_index_ready",
            bool(tag_spatial["ready"]),
            actual=bool(tag_spatial["ready"]),
            expected=True,
            severity=severity,
        )
        _add(
            checks,
            "packed_tags_ready",
            bool(hydration["packed_tags_ready"]),
            actual=bool(hydration["packed_tags_ready"]),
            expected=True,
            severity=severity,
        )

        mission: dict[str, Any] | None = None
        if packages_root is not None:
            scan = scan_mission_queries(packages_root)
            audit = audit_occurrences(scan)
            mission = {
                "packages_root": audit.packages_root,
                "profile_name": audit.profile_name,
                "mission_profile_hash": audit.mission_profile_hash,
                "query_occurrences": audit.query_occurrences,
                "unique_queries": audit.unique_queries,
                "selector_branches": audit.selector_branches,
                "covered_branches": audit.covered_branches,
                "uncovered_branches": audit.uncovered_branches,
                "packages": list(audit.packages),
                "audit_ok": audit.ok,
            }
            _add(checks, "mission_audit", audit.ok, actual=audit.uncovered_branches, expected=0)
            stored_hash = metadata.get("mission_profile_hash")
            _add(
                checks,
                "mission_profile_hash",
                stored_hash == audit.mission_profile_hash,
                actual=stored_hash,
                expected=audit.mission_profile_hash,
                detail="The database must match the currently installed H145 mission corpus.",
            )
            stored_unique = metadata.get("mission_unique_queries")
            _add(
                checks,
                "mission_unique_queries",
                stored_unique == str(audit.unique_queries),
                actual=stored_unique,
                expected=str(audit.unique_queries),
            )
            stored_branches = metadata.get("mission_selector_branches")
            _add(
                checks,
                "mission_selector_branches",
                stored_branches == str(audit.selector_branches),
                actual=stored_branches,
                expected=str(audit.selector_branches),
            )
    finally:
        con.close()

    return ValidationReport(
        database=str(path),
        generated_at=datetime.now(timezone.utc).isoformat(),
        file_size_bytes=path.stat().st_size,
        quick_check=quick,
        metadata=metadata,
        counts=counts,
        derived=derived,
        mission=mission,
        checks=checks,
    )


def report_dict(report: ValidationReport) -> dict[str, Any]:
    return {
        "schema": 1,
        "status": "PASS" if report.ok else "FAIL",
        "warnings": report.warnings,
        "database": report.database,
        "generated_at": report.generated_at,
        "file_size_bytes": report.file_size_bytes,
        "quick_check": report.quick_check,
        "metadata": report.metadata,
        "counts": report.counts,
        "derived": report.derived,
        "mission": report.mission,
        "checks": [asdict(check) for check in report.checks],
    }


def _markdown(report: ValidationReport) -> str:
    lines = [
        "# H145 Local OSM Database Acceptance",
        "",
        f"**Status:** {'PASS' if report.ok else 'FAIL'}",
        f"**Database:** `{report.database}`",
        f"**SQLite quick_check:** `{report.quick_check}`",
        f"**Warnings:** {report.warnings}",
        "",
        "## Checks",
        "",
        "| Check | Status | Actual | Expected |",
        "| --- | --- | --- | --- |",
    ]
    for check in report.checks:
        status = "PASS" if check.ok else ("WARN" if check.severity == "warning" else "FAIL")
        lines.append(
            f"| {check.name} | {status} | `{check.actual}` | `{check.expected}` |"
        )
    lines.extend(["", "## Counts", ""])
    for key, value in sorted(report.counts.items()):
        lines.append(f"- {key}: {value:,}")
    if report.mission is not None:
        lines.extend([
            "",
            "## H145 mission profile",
            "",
            f"- Hash: `{report.mission['mission_profile_hash']}`",
            f"- Unique queries: {report.mission['unique_queries']}",
            f"- Selector branches: {report.mission['selector_branches']}",
            f"- Uncovered branches: {report.mission['uncovered_branches']}",
        ])
    return "\n".join(lines).rstrip() + "\n"


def write_reports(report: ValidationReport, output_dir: str | Path) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "database_validation.json"
    md_path = output / "database_validation.md"
    json_path.write_text(json.dumps(report_dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate one built H145 Local OSM SQLite database read-only.")
    parser.add_argument("--db", type=Path, required=True, help="Explicit database file to validate.")
    parser.add_argument("--packages-root", type=Path, help="Optional MSFS Community/Packages root for mission fingerprint validation.")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--allow-missing-derived", action="store_true", help="Downgrade missing derived acceleration structures to warnings.")
    args = parser.parse_args(argv)

    try:
        report = validate_database(
            args.db,
            packages_root=args.packages_root,
            require_derived=not args.allow_missing_derived,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[db-validate] FAIL: {exc}")
        return 1

    paths = write_reports(report, args.output_dir)
    print(
        f"[db-validate] {'PASS' if report.ok else 'FAIL'} "
        f"quick_check={report.quick_check} objects={report.counts.get('objects', 0):,} "
        f"warnings={report.warnings}"
    )
    for check in report.checks:
        if not check.ok:
            print(f"[db-validate] {check.severity.upper()} {check.name}: actual={check.actual!r} expected={check.expected!r}")
    for path in paths:
        print(f"[db-validate] wrote {path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
