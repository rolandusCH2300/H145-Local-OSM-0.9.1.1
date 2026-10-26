from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .database_benchmark import (
    BenchmarkReport,
    benchmark_database,
    choose_benchmark_anchor,
    report_dict,
    write_reports,
)


DEFAULT_MAX_REGRESSION_PCT = 10.0


@dataclass(frozen=True, slots=True)
class ComparisonCheck:
    name: str
    ok: bool
    actual: Any = None
    expected: Any = None
    detail: str = ""


@dataclass(slots=True)
class BenchmarkComparison:
    old_database: str
    new_database: str
    packages_root: str
    repeat: int
    radius_m: float
    lat: float
    lon: float
    anchor_source: str
    max_regression_pct: float
    old_report: BenchmarkReport
    new_report: BenchmarkReport
    checks: list[ComparisonCheck]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _regression_pct(old_value: float, new_value: float) -> float:
    old_value = float(old_value)
    new_value = float(new_value)
    if old_value <= 0.0:
        return 0.0 if new_value <= 0.0 else math.inf
    return ((new_value / old_value) - 1.0) * 100.0


def _within_regression_limit(old_value: float, new_value: float, limit_pct: float) -> bool:
    if old_value <= 0.0:
        return new_value <= 0.0
    return new_value <= old_value * (1.0 + float(limit_pct) / 100.0)


def evaluate_reports(
    old_report: BenchmarkReport,
    new_report: BenchmarkReport,
    *,
    max_regression_pct: float = DEFAULT_MAX_REGRESSION_PCT,
) -> list[ComparisonCheck]:
    """Evaluate semantic query parity and broad performance-regression gates."""
    if max_regression_pct < 0.0 or max_regression_pct > 100.0:
        raise ValueError("max_regression_pct must be between 0 and 100")

    checks: list[ComparisonCheck] = []
    checks.append(ComparisonCheck(
        "old_benchmark_pass",
        old_report.ok,
        actual=old_report.ok,
        expected=True,
    ))
    checks.append(ComparisonCheck(
        "new_benchmark_pass",
        new_report.ok,
        actual=new_report.ok,
        expected=True,
    ))
    checks.append(ComparisonCheck(
        "mission_profile_hash",
        old_report.mission_profile_hash == new_report.mission_profile_hash,
        actual=new_report.mission_profile_hash,
        expected=old_report.mission_profile_hash,
    ))

    old_by_name = {result.name: result for result in old_report.results}
    new_by_name = {result.name: result for result in new_report.results}
    old_names = tuple(sorted(old_by_name))
    new_names = tuple(sorted(new_by_name))
    checks.append(ComparisonCheck(
        "benchmark_case_set",
        old_names == new_names,
        actual=list(new_names),
        expected=list(old_names),
    ))

    mismatches: list[dict[str, Any]] = []
    for name in sorted(set(old_by_name) & set(new_by_name)):
        old = old_by_name[name]
        new = new_by_name[name]
        if old.query != new.query or old.result_count != new.result_count:
            mismatches.append({
                "case": name,
                "old_results": old.result_count,
                "new_results": new.result_count,
                "query_equal": old.query == new.query,
                "query": new.query if old.query == new.query else None,
                "old_query": old.query if old.query != new.query else None,
                "new_query": new.query if old.query != new.query else None,
                "sources": list(new.sources),
            })
    checks.append(ComparisonCheck(
        "query_result_counts",
        not mismatches and old_names == new_names,
        actual=mismatches,
        expected=[],
        detail="Every materialized benchmark query must return the same result count.",
    ))

    old_summary = report_dict(old_report)["summary"]
    new_summary = report_dict(new_report)["summary"]

    old_p95 = float(old_summary["p95_query_average_ms"])
    new_p95 = float(new_summary["p95_query_average_ms"])
    checks.append(ComparisonCheck(
        "p95_query_average_regression",
        _within_regression_limit(old_p95, new_p95, max_regression_pct),
        actual={
            "old_ms": old_p95,
            "new_ms": new_p95,
            "regression_pct": round(_regression_pct(old_p95, new_p95), 3),
        },
        expected=f"<= {max_regression_pct:g}% regression",
    ))

    old_total = float(old_report.total_elapsed_ms)
    new_total = float(new_report.total_elapsed_ms)
    checks.append(ComparisonCheck(
        "total_elapsed_regression",
        _within_regression_limit(old_total, new_total, max_regression_pct),
        actual={
            "old_ms": old_total,
            "new_ms": new_total,
            "regression_pct": round(_regression_pct(old_total, new_total), 3),
        },
        expected=f"<= {max_regression_pct:g}% regression",
    ))
    return checks


def compare_databases(
    old_db: str | Path,
    new_db: str | Path,
    packages_root: str | Path,
    *,
    repeat: int = 7,
    radius_m: float = 5000.0,
    lat: float | None = None,
    lon: float | None = None,
    max_regression_pct: float = DEFAULT_MAX_REGRESSION_PCT,
) -> BenchmarkComparison:
    """Run the same mission-query benchmark against old and new databases."""
    old_path = Path(old_db).expanduser().resolve()
    new_path = Path(new_db).expanduser().resolve()
    root = Path(packages_root).expanduser().resolve()
    if not old_path.is_file():
        raise FileNotFoundError(old_path)
    if not new_path.is_file():
        raise FileNotFoundError(new_path)
    if not root.is_dir():
        raise FileNotFoundError(root)
    if (lat is None) != (lon is None):
        raise ValueError("lat and lon must either both be supplied or both omitted")

    if lat is None:
        lat, lon, source = choose_benchmark_anchor(new_path)
        anchor_source = f"new:{source}"
    else:
        assert lon is not None
        lat = float(lat)
        lon = float(lon)
        anchor_source = "explicit"

    # Both databases intentionally receive the same explicit coordinates after
    # anchor resolution so schema/layout differences cannot move the workload.
    old_report = benchmark_database(
        old_path,
        root,
        repeat=repeat,
        radius_m=radius_m,
        lat=float(lat),
        lon=float(lon),
        include_is_in=True,
    )
    new_report = benchmark_database(
        new_path,
        root,
        repeat=repeat,
        radius_m=radius_m,
        lat=float(lat),
        lon=float(lon),
        include_is_in=True,
    )
    checks = evaluate_reports(
        old_report,
        new_report,
        max_regression_pct=max_regression_pct,
    )
    return BenchmarkComparison(
        old_database=str(old_path),
        new_database=str(new_path),
        packages_root=str(root),
        repeat=int(repeat),
        radius_m=float(radius_m),
        lat=float(lat),
        lon=float(lon),
        anchor_source=anchor_source,
        max_regression_pct=float(max_regression_pct),
        old_report=old_report,
        new_report=new_report,
        checks=checks,
    )


def comparison_dict(comparison: BenchmarkComparison) -> dict[str, Any]:
    old_data = report_dict(comparison.old_report)
    new_data = report_dict(comparison.new_report)
    old_summary = old_data["summary"]
    new_summary = new_data["summary"]
    return {
        "schema": 1,
        "status": "PASS" if comparison.ok else "FAIL",
        "old_database": comparison.old_database,
        "new_database": comparison.new_database,
        "packages_root": comparison.packages_root,
        "repeat": comparison.repeat,
        "radius_m": comparison.radius_m,
        "lat": comparison.lat,
        "lon": comparison.lon,
        "anchor_source": comparison.anchor_source,
        "max_regression_pct": comparison.max_regression_pct,
        "old_summary": old_summary,
        "new_summary": new_summary,
        "performance": {
            "p50_regression_pct": round(_regression_pct(
                float(old_summary["p50_query_average_ms"]),
                float(new_summary["p50_query_average_ms"]),
            ), 3),
            "p95_regression_pct": round(_regression_pct(
                float(old_summary["p95_query_average_ms"]),
                float(new_summary["p95_query_average_ms"]),
            ), 3),
            "slowest_regression_pct": round(_regression_pct(
                float(old_summary["slowest_query_average_ms"]),
                float(new_summary["slowest_query_average_ms"]),
            ), 3),
            "total_elapsed_regression_pct": round(_regression_pct(
                comparison.old_report.total_elapsed_ms,
                comparison.new_report.total_elapsed_ms,
            ), 3),
        },
        "checks": [asdict(check) for check in comparison.checks],
    }


def _markdown(comparison: BenchmarkComparison) -> str:
    data = comparison_dict(comparison)
    old = data["old_summary"]
    new = data["new_summary"]
    perf = data["performance"]
    lines = [
        "# H145 Local OSM Old-vs-New Query Benchmark",
        "",
        f"**Status:** {data['status']}",
        f"**Old DB:** `{comparison.old_database}`",
        f"**New DB:** `{comparison.new_database}`",
        f"**Location:** {comparison.lat:.7f}, {comparison.lon:.7f} ({comparison.anchor_source})",
        f"**Radius:** {comparison.radius_m:g} m",
        f"**Runs per query:** {comparison.repeat}",
        f"**Allowed regression:** {comparison.max_regression_pct:g}% for p95 and total elapsed",
        "",
        "| Metric | Old | New | Change |",
        "| --- | ---: | ---: | ---: |",
        f"| p50 query average | {old['p50_query_average_ms']:.3f} ms | {new['p50_query_average_ms']:.3f} ms | {perf['p50_regression_pct']:+.3f}% |",
        f"| p95 query average | {old['p95_query_average_ms']:.3f} ms | {new['p95_query_average_ms']:.3f} ms | {perf['p95_regression_pct']:+.3f}% |",
        f"| Slowest query average | {old['slowest_query_average_ms']:.3f} ms | {new['slowest_query_average_ms']:.3f} ms | {perf['slowest_regression_pct']:+.3f}% |",
        f"| Total elapsed | {comparison.old_report.total_elapsed_ms / 1000.0:.3f} s | {comparison.new_report.total_elapsed_ms / 1000.0:.3f} s | {perf['total_elapsed_regression_pct']:+.3f}% |",
        "",
        "## Gates",
        "",
    ]
    for check in comparison.checks:
        lines.append(f"- {'PASS' if check.ok else 'FAIL'} `{check.name}`: {check.actual}")
    return "\n".join(lines).rstrip() + "\n"


def write_comparison_reports(
    comparison: BenchmarkComparison,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    write_reports(comparison.old_report, output / "old")
    write_reports(comparison.new_report, output / "new")
    json_path = output / "database_benchmark_comparison.json"
    md_path = output / "database_benchmark_comparison.md"
    json_path.write_text(
        json.dumps(comparison_dict(comparison), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown(comparison), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark old and new H145 Local OSM databases with identical mission-query workloads."
    )
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--packages-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("reports") / "benchmark-compare")
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--radius", type=float, default=5000.0)
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--max-regression-pct", type=float, default=DEFAULT_MAX_REGRESSION_PCT)
    args = parser.parse_args(argv)

    try:
        comparison = compare_databases(
            args.old,
            args.new,
            args.packages_root,
            repeat=args.repeat,
            radius_m=args.radius,
            lat=args.lat,
            lon=args.lon,
            max_regression_pct=args.max_regression_pct,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[db-benchmark-compare] FAIL: {exc}")
        return 1

    paths = write_comparison_reports(comparison, args.output_dir)
    data = comparison_dict(comparison)
    perf = data["performance"]
    print(
        f"[db-benchmark-compare] {'PASS' if comparison.ok else 'FAIL'} "
        f"p95={perf['p95_regression_pct']:+.2f}% total={perf['total_elapsed_regression_pct']:+.2f}% "
        f"limit=+{comparison.max_regression_pct:g}%"
    )
    for check in comparison.checks:
        print(
            f"[db-benchmark-compare] {'PASS' if check.ok else 'FAIL'} "
            f"{check.name}: {check.actual}"
        )
    for path in paths:
        print(f"[db-benchmark-compare] wrote {path}")
    return 0 if comparison.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
