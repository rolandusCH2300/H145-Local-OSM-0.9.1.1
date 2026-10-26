from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import open_existing_query_db
from .mission_audit import audit_occurrences, scan_mission_queries
from .query import QueryProfile, UnsupportedQuery, execute_overpass_subset, parse_selectors


DEFAULT_REPEAT = 3
DEFAULT_RADIUS_M = 5000.0


@dataclass(slots=True)
class BenchmarkResult:
    name: str
    query: str
    sources: tuple[str, ...]
    features: tuple[str, ...]
    executions: int
    samples_ms: list[float] = field(default_factory=list)
    average_ms: float = 0.0
    median_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    result_count: int = 0
    profile_average: dict[str, float] = field(default_factory=dict)
    error: str | None = None


@dataclass(slots=True)
class BenchmarkReport:
    database: str
    packages_root: str
    generated_at: str
    mission_profile_hash: str
    mission_query_occurrences: int
    unique_mission_queries: int
    benchmark_cases: int
    repeat: int
    radius_m: float
    lat: float
    lon: float
    anchor_source: str
    total_executions: int
    total_elapsed_ms: float
    results: list[BenchmarkResult]
    database_metadata: dict[str, str]

    @property
    def ok(self) -> bool:
        return all(result.error is None for result in self.results)


def classify_query(query: str) -> tuple[str, ...]:
    """Return stable execution-shape labels for benchmark reporting."""
    lower = query.lower()
    features: set[str] = set()
    if "is_in(" in lower:
        features.add("is_in")
    if "node(w.major)(w.minor)" in lower.replace(" ", ""):
        features.add("road_intersection")
    if re.search(r"\(\s*\._\s*;\s*>\s*;\s*\)", query, re.IGNORECASE):
        features.add("recurse")
    if "around:" in lower:
        features.add("around")

    try:
        selectors = parse_selectors(query)
    except UnsupportedQuery:
        selectors = []
    if len(selectors) > 1:
        features.add("union")
    for selector in selectors:
        features.add(selector.kind.lower())
        if selector.spatial_mode == "around":
            features.add("around")
        else:
            features.add("bbox")
        for tag_filter in selector.filters:
            if tag_filter.op == "exists":
                features.add("exists")
            elif tag_filter.op == "~":
                features.add("regex")
            elif tag_filter.op == "!=":
                features.add("negative")
            elif tag_filter.op == "=":
                features.add("exact")
    return tuple(sorted(features))


def choose_benchmark_anchor(db_path: str | Path) -> tuple[float, float, str]:
    """Choose a populated benchmark point from the selected database.

    Prefer a hospital, then a helipad, because both are common H145 mission
    anchors and normally sit in mapped/populated areas. Fall back to the first
    tagged retained object. This keeps the shipped benchmark country-agnostic.
    """
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    con = open_existing_query_db(path)
    try:
        for key, value in (("amenity", "hospital"), ("aeroway", "helipad")):
            row = con.execute(
                "SELECT o.center_lat,o.center_lon "
                "FROM object_tags t INDEXED BY idx_tags_key_value "
                "JOIN objects o ON o.pk=t.object_pk "
                "WHERE t.key=? AND t.value=? ORDER BY t.object_pk LIMIT 1",
                (key, value),
            ).fetchone()
            if row is not None:
                return float(row[0]), float(row[1]), f"{key}={value}"

        row = con.execute(
            "SELECT o.center_lat,o.center_lon "
            "FROM object_tags t JOIN objects o ON o.pk=t.object_pk "
            "ORDER BY t.object_pk LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("database contains no tagged objects for a benchmark anchor")
        return float(row[0]), float(row[1]), "first_tagged_object"
    finally:
        con.close()


def _materialize_query(query: str, radius_m: float, lat: float, lon: float) -> str:
    around = f"around:{float(radius_m):g},{float(lat):.7f},{float(lon):.7f}"
    return query.replace("{{bbox}}", around)


def _profile_average(profiles: list[dict[str, float | int]]) -> dict[str, float]:
    keys = sorted({key for profile in profiles for key in profile})
    out: dict[str, float] = {}
    for key in keys:
        values = [float(profile.get(key, 0.0)) for profile in profiles]
        out[key] = round(statistics.fmean(values), 3)
    return out


def _read_metadata(con: sqlite3.Connection) -> dict[str, str]:
    try:
        return {str(row[0]): str(row[1]) for row in con.execute("SELECT key,value FROM metadata")}
    except sqlite3.Error:
        return {}


def _query_cases(
    packages_root: str | Path,
    *,
    radius_m: float,
    lat: float,
    lon: float,
    include_is_in: bool,
) -> tuple[Any, list[tuple[str, str, tuple[str, ...]]]]:
    scan = scan_mission_queries(packages_root)
    audit = audit_occurrences(scan)
    if not audit.ok:
        problems = []
        if audit.missing_requirements:
            problems.append(f"missing selectors={len(audit.missing_requirements)}")
        if audit.parse_errors:
            problems.append(f"parse errors={len(audit.parse_errors)}")
        if audit.scan_errors:
            problems.append(f"scan errors={len(audit.scan_errors)}")
        raise ValueError("H145 mission audit failed: " + ", ".join(problems))

    source_map: dict[str, set[str]] = {}
    for occurrence in scan.occurrences:
        source_map.setdefault(occurrence.query.strip(), set()).add(
            f"{occurrence.package}: {occurrence.path}"
        )

    cases: list[tuple[str, str, tuple[str, ...]]] = []
    for index, raw_query in enumerate(sorted(source_map), start=1):
        query = _materialize_query(raw_query, radius_m, lat, lon)
        cases.append(
            (
                f"mission_{index:03d}",
                query,
                tuple(sorted(source_map[raw_query])),
            )
        )

    if include_is_in:
        query = (
            f"[out:json];is_in({float(lat):.7f},{float(lon):.7f})->.a;"
            "rel(pivot.a)[boundary=administrative][admin_level=2];out tags;"
        )
        cases.append(("api_is_in_country", query, ("H145 Local OSM API compatibility",)))

    return audit, cases


def benchmark_database(
    db_path: str | Path,
    packages_root: str | Path,
    *,
    repeat: int = DEFAULT_REPEAT,
    radius_m: float = DEFAULT_RADIUS_M,
    lat: float | None = None,
    lon: float | None = None,
    include_is_in: bool = True,
) -> BenchmarkReport:
    """Benchmark every distinct installed H145 mission query sequentially.

    This intentionally uses one persistent query-only connection and no
    concurrency. Each case is executed exactly ``repeat`` times. The wall time
    includes parsing, SQLite work, filtering and result hydration, but excludes
    HTTP/JSON wire serialization so the report isolates database/query-engine
    performance.
    """
    repeat = int(repeat)
    if repeat < 1 or repeat > 10:
        raise ValueError("repeat must be between 1 and 10")
    if radius_m <= 0 or radius_m > 100_000:
        raise ValueError("radius_m must be >0 and <=100000 for the diagnostic benchmark")

    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    root = Path(packages_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)

    if (lat is None) != (lon is None):
        raise ValueError("lat and lon must either both be supplied or both omitted")
    if lat is None and lon is None:
        lat, lon, anchor_source = choose_benchmark_anchor(path)
    else:
        assert lat is not None and lon is not None
        lat = float(lat)
        lon = float(lon)
        anchor_source = "explicit"

    audit, cases = _query_cases(
        root,
        radius_m=radius_m,
        lat=lat,
        lon=lon,
        include_is_in=include_is_in,
    )

    con = open_existing_query_db(path)
    metadata = _read_metadata(con)
    started_all = time.perf_counter()
    results: list[BenchmarkResult] = []
    try:
        for case_index, (name, query, sources) in enumerate(cases, start=1):
            samples: list[float] = []
            profiles: list[dict[str, float | int]] = []
            result_counts: list[int] = []
            error: str | None = None

            for _ in range(repeat):
                profile = QueryProfile()
                started = time.perf_counter()
                try:
                    elements = execute_overpass_subset(con, query, profile=profile)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    break
                wall_ms = (time.perf_counter() - started) * 1000.0
                profile.unprofiled_ms = max(0.0, wall_ms - profile.timed_ms())
                samples.append(wall_ms)
                profiles.append(profile.as_dict(include_coverage=True))
                result_counts.append(len(elements))

            if samples:
                average = statistics.fmean(samples)
                median = statistics.median(samples)
                min_ms = min(samples)
                max_ms = max(samples)
            else:
                average = median = min_ms = max_ms = 0.0

            result = BenchmarkResult(
                name=name,
                query=query,
                sources=sources,
                features=classify_query(query),
                executions=len(samples),
                samples_ms=[round(value, 3) for value in samples],
                average_ms=round(average, 3),
                median_ms=round(median, 3),
                min_ms=round(min_ms, 3),
                max_ms=round(max_ms, 3),
                result_count=result_counts[-1] if result_counts else 0,
                profile_average=_profile_average(profiles) if profiles else {},
                error=error,
            )
            results.append(result)
            print(
                f"[db-benchmark] {case_index:03d}/{len(cases):03d} {name} "
                + (
                    f"avg={result.average_ms:,.2f}ms results={result.result_count:,}"
                    if error is None
                    else f"ERROR {error}"
                )
            )
    finally:
        con.close()

    elapsed_ms = (time.perf_counter() - started_all) * 1000.0
    return BenchmarkReport(
        database=str(path),
        packages_root=str(root),
        generated_at=datetime.now(timezone.utc).isoformat(),
        mission_profile_hash=audit.mission_profile_hash,
        mission_query_occurrences=audit.query_occurrences,
        unique_mission_queries=audit.unique_queries,
        benchmark_cases=len(cases),
        repeat=repeat,
        radius_m=float(radius_m),
        lat=float(lat),
        lon=float(lon),
        anchor_source=anchor_source,
        total_executions=sum(result.executions for result in results),
        total_elapsed_ms=round(elapsed_ms, 3),
        results=results,
        database_metadata=metadata,
    )


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lo = int(position)
    hi = min(lo + 1, len(ordered) - 1)
    fraction = position - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * fraction


def report_dict(report: BenchmarkReport) -> dict[str, Any]:
    averages = [result.average_ms for result in report.results if result.error is None]
    errors = [result for result in report.results if result.error is not None]
    return {
        "schema": 1,
        "status": "PASS" if report.ok else "FAIL",
        "database": report.database,
        "packages_root": report.packages_root,
        "generated_at": report.generated_at,
        "mission_profile_hash": report.mission_profile_hash,
        "mission_query_occurrences": report.mission_query_occurrences,
        "unique_mission_queries": report.unique_mission_queries,
        "benchmark_cases": report.benchmark_cases,
        "repeat": report.repeat,
        "radius_m": report.radius_m,
        "lat": report.lat,
        "lon": report.lon,
        "anchor_source": report.anchor_source,
        "total_executions": report.total_executions,
        "total_elapsed_ms": report.total_elapsed_ms,
        "summary": {
            "successful_cases": len(averages),
            "failed_cases": len(errors),
            "average_of_query_averages_ms": round(statistics.fmean(averages), 3) if averages else 0.0,
            "p50_query_average_ms": round(_percentile(averages, 0.50), 3),
            "p95_query_average_ms": round(_percentile(averages, 0.95), 3),
            "slowest_query_average_ms": round(max(averages), 3) if averages else 0.0,
        },
        "database_metadata": report.database_metadata,
        "results": [asdict(result) for result in report.results],
    }


def _markdown(report: BenchmarkReport) -> str:
    data = report_dict(report)
    summary = data["summary"]
    lines = [
        "# H145 Local OSM Database Performance Benchmark",
        "",
        f"**Status:** {data['status']}",
        f"**Database:** `{report.database}`",
        f"**Mission profile:** `{report.mission_profile_hash}`",
        f"**Distinct mission templates:** {report.unique_mission_queries}",
        f"**Benchmark cases:** {report.benchmark_cases}",
        f"**Executions per case:** {report.repeat}",
        f"**Materialized radius:** {report.radius_m:g} m",
        f"**Location:** {report.lat:.7f}, {report.lon:.7f} ({report.anchor_source})",
        f"**Total measured executions:** {report.total_executions}",
        f"**Total elapsed:** {report.total_elapsed_ms / 1000.0:,.2f} s",
        "",
        "## Summary",
        "",
        f"- Successful cases: {summary['successful_cases']}",
        f"- Failed cases: {summary['failed_cases']}",
        f"- Mean of per-query averages: {summary['average_of_query_averages_ms']:,.2f} ms",
        f"- p50 per-query average: {summary['p50_query_average_ms']:,.2f} ms",
        f"- p95 per-query average: {summary['p95_query_average_ms']:,.2f} ms",
        f"- Slowest per-query average: {summary['slowest_query_average_ms']:,.2f} ms",
        "",
        "## Per-query results",
        "",
        "| Case | Avg ms | Runs | Results | Features |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for result in sorted(report.results, key=lambda item: item.average_ms, reverse=True):
        avg = f"{result.average_ms:,.2f}" if result.error is None else "ERROR"
        features = ", ".join(result.features)
        lines.append(
            f"| {result.name} | {avg} | {result.executions} | {result.result_count:,} | {features} |"
        )
    failed = [result for result in report.results if result.error]
    if failed:
        lines.extend(["", "## Errors", ""])
        for result in failed:
            lines.append(f"- `{result.name}`: {result.error}")
    lines.extend([
        "",
        "The timing is direct database/query-engine wall time. It includes query parsing, SQLite work, filtering and result hydration, but excludes HTTP and JSON serialization overhead.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def write_reports(report: BenchmarkReport, output_dir: str | Path) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "database_benchmark.json"
    md_path = output / "database_benchmark.md"
    json_path.write_text(json.dumps(report_dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark every distinct installed H145 mission OSM query against one explicit SQLite DB."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--packages-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="Measured runs per query (default: 3, max: 10).")
    parser.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M, help="Metres used to materialize {{bbox}} (default: 5000).")
    parser.add_argument("--lat", type=float, help="Optional benchmark latitude; omit with --lon to auto-select from the DB.")
    parser.add_argument("--lon", type=float, help="Optional benchmark longitude; omit with --lat to auto-select from the DB.")
    parser.add_argument("--no-is-in", action="store_true", help="Do not add the API is_in country-containment compatibility case.")
    args = parser.parse_args(argv)

    try:
        report = benchmark_database(
            args.db,
            args.packages_root,
            repeat=args.repeat,
            radius_m=args.radius,
            lat=args.lat,
            lon=args.lon,
            include_is_in=not args.no_is_in,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"[db-benchmark] FAIL: {exc}")
        return 1

    paths = write_reports(report, args.output_dir)
    data = report_dict(report)
    summary = data["summary"]
    print(
        f"[db-benchmark] {'PASS' if report.ok else 'FAIL'} cases={report.benchmark_cases} "
        f"executions={report.total_executions} anchor={report.lat:.5f},{report.lon:.5f} "
        f"p50={summary['p50_query_average_ms']:,.2f}ms "
        f"p95={summary['p95_query_average_ms']:,.2f}ms slowest={summary['slowest_query_average_ms']:,.2f}ms"
    )
    for path in paths:
        print(f"[db-benchmark] wrote {path}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
