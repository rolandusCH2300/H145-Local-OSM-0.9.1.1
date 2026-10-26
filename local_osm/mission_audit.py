from __future__ import annotations

"""Audit installed HPG/H145 mission OSM queries against the import profile.

The Mission System merges mission JSON from every installed ``hpgmission``
folder.  This module mirrors that discovery model, extracts literal Overpass
queries, and verifies that every spatial selector has at least one tag filter
that guarantees the object survives import pruning.
"""

import argparse
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .import_profile import ANY_VALUE_KEYS, EXACT_VALUES, PROFILE_NAME
from .query import TagFilter, UnsupportedQuery, parse_selectors

_BBOX_TOKEN = "{{bbox}}"
_AUDIT_BBOX = "0,0,1,1"
_SIMPLE_FINITE_REGEX_RE = re.compile(
    r"^\^?\(?([A-Za-z0-9_:.-]+(?:\|[A-Za-z0-9_:.-]+)+)\)?\$?$"
)


@dataclass(frozen=True, slots=True)
class MissionQueryOccurrence:
    package: str
    path: str
    json_path: str
    query: str


@dataclass(frozen=True, slots=True)
class ScanError:
    path: str
    error: str


@dataclass(slots=True)
class MissionScan:
    packages_root: str
    files_scanned: int = 0
    files_with_osm: int = 0
    occurrences: list[MissionQueryOccurrence] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MissingRequirement:
    mode: str  # exact, any, all
    key: str
    values: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryParseError:
    package: str
    path: str
    json_path: str
    query: str
    error: str


@dataclass(slots=True)
class MissionAuditResult:
    packages_root: str
    profile_name: str
    files_scanned: int
    files_with_osm: int
    packages: tuple[str, ...]
    query_occurrences: int
    unique_queries: int
    selector_branches: int
    covered_branches: int
    uncovered_branches: int
    required_any_keys: tuple[str, ...]
    required_exact_values: dict[str, tuple[str, ...]]
    missing_requirements: list[MissingRequirement]
    parse_errors: list[QueryParseError]
    scan_errors: list[ScanError]
    mission_profile_hash: str
    occurrences: list[MissionQueryOccurrence]
    excluded_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_requirements and not self.parse_errors and not self.scan_errors


def _json_path_key(base: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{base}.{key}"
    return f"{base}[{json.dumps(key, ensure_ascii=False)}]"


def _iter_strings(value, path: str = "$"):
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_strings(child, _json_path_key(path, str(key)))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_strings(child, f"{path}[{index}]")


def _is_hpgmission_json(root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = [part.lower() for part in rel.parts[:-1]]
    return "hpgmission" in parts and path.suffix.lower() == ".json"


def scan_mission_queries(
    packages_root: str | Path, *, excluded_files: Iterable[str | Path] = (),
) -> MissionScan:
    """Scan every installed ``hpgmission`` JSON for literal Overpass queries."""
    root = Path(packages_root).expanduser().resolve()
    result = MissionScan(packages_root=str(root))
    excluded = {(root / path).resolve() for path in excluded_files}

    if not root.exists():
        result.errors.append(ScanError(str(root), "packages root does not exist"))
        return result

    for path in sorted(root.rglob("*.json")):
        if not _is_hpgmission_json(root, path):
            continue
        if path.resolve() in excluded:
            result.excluded_files.append(str(path.relative_to(root)))
            continue
        result.files_scanned += 1
        rel = path.relative_to(root)
        package = rel.parts[0] if rel.parts else path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            result.errors.append(ScanError(str(rel), f"{type(exc).__name__}: {exc}"))
            continue

        found = 0
        for json_path, value in _iter_strings(payload):
            if "[out:json]" not in value.lower():
                continue
            found += 1
            result.occurrences.append(MissionQueryOccurrence(
                package=package,
                path=str(rel),
                json_path=json_path,
                query=value.strip(),
            ))
        if found:
            result.files_with_osm += 1

    return result


def _finite_regex_values(pattern: str | None) -> tuple[str, ...] | None:
    if not pattern:
        return None
    match = _SIMPLE_FINITE_REGEX_RE.fullmatch(pattern.strip())
    if not match:
        return None
    values = tuple(sorted(set(match.group(1).split("|"))))
    return values or None


def _requirement_for_filter(tag_filter: TagFilter) -> tuple[str, str, tuple[str, ...]]:
    key = tag_filter.key
    if tag_filter.op == "=":
        return "exact", key, (tag_filter.value or "",)
    if tag_filter.op == "~":
        finite = _finite_regex_values(tag_filter.value)
        if finite is not None:
            return "exact", key, finite
        return "any", key, ()
    # An existence or negative filter can match arbitrary values for the key.
    return "any", key, ()


def _requirement_is_covered(
    requirement: tuple[str, str, tuple[str, ...]],
    any_value_keys: set[str],
    exact_values: Mapping[str, set[str]],
) -> bool:
    mode, key, values = requirement
    if key in any_value_keys:
        return True
    if mode != "exact":
        return False
    retained = exact_values.get(key, set())
    return bool(values) and all(value in retained for value in values)


def _requirement_rank(requirement: tuple[str, str, tuple[str, ...]]) -> tuple[int, int, str]:
    mode, key, values = requirement
    # Prefer the narrowest safe anchor so secondary filters such as [name] do
    # not accidentally broaden the imported database.
    if mode == "exact":
        return (0, len(values), key)
    if mode == "any":
        return (1, 0, key)
    return (2, 0, key)


def _source_label(occurrence: MissionQueryOccurrence, selector_index: int) -> str:
    return f"{occurrence.package}: {occurrence.path} {occurrence.json_path} selector#{selector_index + 1}"


def audit_occurrences(
    scan: MissionScan,
    *,
    any_value_keys: Iterable[str] = ANY_VALUE_KEYS,
    exact_values: Mapping[str, set[str]] = EXACT_VALUES,
    profile_name: str = PROFILE_NAME,
) -> MissionAuditResult:
    """Compare extracted mission selectors with the current import retention profile."""
    any_keys = set(any_value_keys)
    exact = {str(key): set(values) for key, values in exact_values.items()}

    selector_branches = 0
    covered_branches = 0
    parse_errors: list[QueryParseError] = []
    required_any: set[str] = set()
    required_exact: dict[str, set[str]] = {}
    missing_sources: dict[tuple[str, str, tuple[str, ...]], set[str]] = {}

    for occurrence in scan.occurrences:
        runtime_query = occurrence.query.replace(_BBOX_TOKEN, _AUDIT_BBOX)
        try:
            selectors = parse_selectors(runtime_query)
        except UnsupportedQuery as exc:
            parse_errors.append(QueryParseError(
                occurrence.package,
                occurrence.path,
                occurrence.json_path,
                occurrence.query,
                str(exc),
            ))
            continue

        if not selectors:
            parse_errors.append(QueryParseError(
                occurrence.package,
                occurrence.path,
                occurrence.json_path,
                occurrence.query,
                "no supported spatial selector found",
            ))
            continue

        for selector_index, selector in enumerate(selectors):
            selector_branches += 1
            requirements = [_requirement_for_filter(item) for item in selector.filters]
            covered = [
                requirement
                for requirement in requirements
                if _requirement_is_covered(requirement, any_keys, exact)
            ]

            if covered:
                covered_branches += 1
                chosen = min(covered, key=_requirement_rank)
            elif requirements:
                chosen = min(requirements, key=_requirement_rank)
                missing_sources.setdefault(chosen, set()).add(
                    _source_label(occurrence, selector_index)
                )
            else:
                chosen = ("all", selector.kind, ())
                missing_sources.setdefault(chosen, set()).add(
                    _source_label(occurrence, selector_index)
                )

            mode, key, values = chosen
            if mode == "any":
                required_any.add(key)
            elif mode == "exact":
                required_exact.setdefault(key, set()).update(values)

    missing_requirements = [
        MissingRequirement(mode, key, values, tuple(sorted(sources)))
        for (mode, key, values), sources in sorted(
            missing_sources.items(), key=lambda item: _requirement_rank(item[0])
        )
    ]

    unique_queries = sorted({item.query.strip() for item in scan.occurrences})
    hash_payload = json.dumps(
        {
            "queries": unique_queries,
            "required_any": sorted(required_any),
            "required_exact": {
                key: sorted(values) for key, values in sorted(required_exact.items())
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return MissionAuditResult(
        packages_root=scan.packages_root,
        profile_name=profile_name,
        files_scanned=scan.files_scanned,
        files_with_osm=scan.files_with_osm,
        packages=tuple(sorted({item.package for item in scan.occurrences})),
        query_occurrences=len(scan.occurrences),
        unique_queries=len(unique_queries),
        selector_branches=selector_branches,
        covered_branches=covered_branches,
        uncovered_branches=selector_branches - covered_branches,
        required_any_keys=tuple(sorted(required_any)),
        required_exact_values={
            key: tuple(sorted(values)) for key, values in sorted(required_exact.items())
        },
        missing_requirements=missing_requirements,
        parse_errors=parse_errors,
        scan_errors=list(scan.errors),
        mission_profile_hash=hashlib.sha256(hash_payload).hexdigest(),
        occurrences=list(scan.occurrences),
        excluded_files=list(scan.excluded_files),
    )


def _result_dict(result: MissionAuditResult) -> dict:
    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "packages_root": result.packages_root,
        "profile_name": result.profile_name,
        "mission_profile_hash": result.mission_profile_hash,
        "packages": list(result.packages),
        "files_scanned": result.files_scanned,
        "excluded_files": list(result.excluded_files),
        "files_with_osm": result.files_with_osm,
        "query_occurrences": result.query_occurrences,
        "unique_queries": result.unique_queries,
        "selector_branches": result.selector_branches,
        "covered_branches": result.covered_branches,
        "uncovered_branches": result.uncovered_branches,
        "required_any_keys": list(result.required_any_keys),
        "required_exact_values": {
            key: list(values) for key, values in result.required_exact_values.items()
        },
        "missing_requirements": [
            {
                "mode": item.mode,
                "key": item.key,
                "values": list(item.values),
                "sources": list(item.sources),
            }
            for item in result.missing_requirements
        ],
        "parse_errors": [
            {
                "package": item.package,
                "path": item.path,
                "json_path": item.json_path,
                "query": item.query,
                "error": item.error,
            }
            for item in result.parse_errors
        ],
        "scan_errors": [
            {"path": item.path, "error": item.error} for item in result.scan_errors
        ],
    }


def _markdown(result: MissionAuditResult) -> str:
    status = "PASS" if result.ok else "FAIL"
    lines = [
        "# H145 Mission OSM Coverage Audit",
        "",
        f"**Status:** {status}",
        "",
        f"- Packages with OSM queries: {len(result.packages)}",
        f"- Mission JSON files scanned: {result.files_scanned}",
        f"- Mission JSON files with OSM: {result.files_with_osm}",
        f"- OSM query occurrences: {result.query_occurrences}",
        f"- Distinct query templates: {result.unique_queries}",
        f"- Selector branches: {result.selector_branches}",
        f"- Covered branches: {result.covered_branches}",
        f"- Uncovered branches: {result.uncovered_branches}",
        f"- Mission profile SHA-256: `{result.mission_profile_hash}`",
        "",
    ]

    if result.missing_requirements:
        lines.extend(["## Missing import requirements", ""])
        for item in result.missing_requirements:
            if item.mode == "exact":
                requirement = f"`{item.key}={ '|'.join(item.values) }`"
            elif item.mode == "any":
                requirement = f"`[{item.key}]` (retain any value)"
            else:
                requirement = f"all `{item.key}` objects"
            lines.append(f"- {requirement}")
            for source in item.sources:
                lines.append(f"  - {source}")
        lines.append("")

    if result.parse_errors:
        lines.extend(["## Unsupported/unparsed mission queries", ""])
        for item in result.parse_errors:
            lines.append(f"- `{item.path}` {item.json_path}: {item.error}")
        lines.append("")

    if result.scan_errors:
        lines.extend(["## Mission JSON read errors", ""])
        for item in result.scan_errors:
            lines.append(f"- `{item.path}`: {item.error}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_reports(result: MissionAuditResult, output_dir: str | Path) -> tuple[Path, Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    queries_path = output / "h145_mission_queries.json"
    coverage_path = output / "h145_mission_coverage.json"
    markdown_path = output / "h145_mission_coverage.md"

    queries_path.write_text(json.dumps(
        [
            {
                "package": item.package,
                "path": item.path,
                "json_path": item.json_path,
                "query": item.query,
            }
            for item in result.occurrences
        ],
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n", encoding="utf-8")

    coverage_path.write_text(
        json.dumps(_result_dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(result), encoding="utf-8")
    return queries_path, coverage_path, markdown_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit installed HPG/H145 mission OSM queries against the local import profile."
    )
    parser.add_argument(
        "--packages-root",
        required=True,
        help="MSFS Packages or Community root containing packages with hpgmission folders.",
    )
    parser.add_argument(
        "--output-dir",
        default="reports",
        help="Directory for JSON/Markdown audit reports (default: reports).",
    )
    parser.add_argument(
        "--allow-uncovered",
        action="store_true",
        help="Return exit code 0 even if missing selectors or parse/read errors are found.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    scan = scan_mission_queries(args.packages_root)
    result = audit_occurrences(scan)
    paths = write_reports(result, args.output_dir)

    print(
        "[mission-audit] "
        f"queries={result.query_occurrences} unique={result.unique_queries} "
        f"branches={result.selector_branches} covered={result.covered_branches} "
        f"uncovered={result.uncovered_branches}"
    )
    for item in result.missing_requirements:
        values = f"={','.join(item.values)}" if item.values else ""
        print(f"[mission-audit] missing {item.mode}: {item.key}{values}")
    for path in paths:
        print(f"[mission-audit] wrote {path}")

    if args.allow_uncovered:
        return 0
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
