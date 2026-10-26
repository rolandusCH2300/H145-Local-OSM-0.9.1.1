from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import geofabrik
from .importer import import_pbf
from .mission_audit import MissionAuditResult, audit_occurrences, scan_mission_queries
from .network_errors import NETWORK_ERRORS, network_failure_message


_TYPE_ORDER = {
    "node": 0,
    "way": 1,
    "relation": 2,
}

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUNDLED_OSMIUM_RELATIVE = Path("tools") / "osmium" / "osmium.exe"

SnapshotSignature = tuple[str | None, str | None]


def resolve_regions(index: dict, queries: Sequence[str]) -> list[dict]:
    """Resolve user selections to unique Geofabrik regions in input order."""
    if not queries:
        raise ValueError("select at least one Geofabrik region")

    resolved: list[dict] = []
    seen_ids: set[str] = set()
    for query in queries:
        region = geofabrik.resolve_region(index, query)
        region_id = str(region["id"])
        if region_id in seen_ids:
            continue
        seen_ids.add(region_id)
        resolved.append(region)
    return resolved


def _selected_country_iso2(regions: Sequence[dict]) -> tuple[str, ...]:
    """Return normalized ISO-2 codes for explicitly selected Geofabrik regions."""
    codes = {
        str(code).strip().upper()
        for region in regions
        for code in (region.get("iso2") or [])
        if str(code).strip()
    }
    return tuple(sorted(codes))


def _mission_audit_metadata(result: MissionAuditResult | Any | None) -> dict[str, str]:
    """Serialize a passing mission audit into stable SQLite metadata values."""
    if result is None:
        return {}
    return {
        "mission_profile_name": str(result.profile_name),
        "mission_profile_hash": str(result.mission_profile_hash),
        "mission_query_occurrences": str(int(result.query_occurrences)),
        "mission_unique_queries": str(int(result.unique_queries)),
        "mission_selector_branches": str(int(result.selector_branches)),
        "mission_files_scanned": str(int(result.files_scanned)),
        "mission_files_with_osm": str(int(result.files_with_osm)),
        "mission_packages": json.dumps(
            sorted(str(package) for package in result.packages),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "mission_packages_root": str(result.packages_root),
        "mission_excluded_files": json.dumps(
            sorted(getattr(result, "excluded_files", [])), ensure_ascii=False,
        ),
    }


def _run_mission_audit_preflight(
    packages_root: str | Path | None,
    *, excluded_files: Sequence[str | Path] = (),
) -> MissionAuditResult | None:
    """Fail before network/download work if installed H145 missions are uncovered."""
    if not packages_root:
        return None

    scan = scan_mission_queries(packages_root, excluded_files=excluded_files)
    for path in scan.excluded_files:
        print(f"[mission-audit] Skipping mission file for this build: {Path(scan.packages_root) / path}")
    result = audit_occurrences(scan)
    print(
        "[mission-audit] preflight "
        f"queries={result.query_occurrences} unique={result.unique_queries} "
        f"branches={result.selector_branches} covered={result.covered_branches} "
        f"uncovered={result.uncovered_branches}"
    )

    if result.ok:
        print(f"[mission-audit] profile={result.mission_profile_hash} [OK]")
        return result

    problems: list[str] = []
    for item in result.missing_requirements:
        if item.mode == "exact":
            suffix = "=" + "|".join(item.values)
        else:
            suffix = ""
        problems.append(f"missing {item.mode} selector {item.key}{suffix}")
    if result.parse_errors:
        problems.append(f"unsupported queries={len(result.parse_errors)}")
    if result.scan_errors:
        problems.append(f"mission JSON read errors={len(result.scan_errors)}")
        for item in result.scan_errors:
            path = Path(result.packages_root) / item.path
            print(f"[mission-audit] JSON read error: {path}: {item.error}")
    detail = "; ".join(problems) or "unknown audit failure"
    raise ValueError(f"H145 mission audit failed before download: {detail}")


def _safe_slug_part(value: str) -> str:
    part = value.rsplit("/", 1)[-1].strip().lower()
    part = re.sub(r"[^a-z0-9]+", "-", part).strip("-")
    return part or "region"


def bundle_slug(regions: Sequence[dict]) -> str:
    """Return a deterministic filesystem-safe name for a region bundle."""
    if not regions:
        raise ValueError("cannot name an empty region bundle")

    parts = sorted(
        _safe_slug_part(
            str(region.get("id") or region.get("name") or "region")
        )
        for region in regions
    )
    plain = "-".join(parts)
    if len(plain) <= 96:
        return plain

    digest = hashlib.sha256(
        "\0".join(parts).encode("utf-8")
    ).hexdigest()[:12]
    prefix = "-".join(parts[:3])[:80].rstrip("-")
    return f"{prefix}-{digest}"


def find_osmium_executable(*, project_root: Path | None = None) -> str | None:
    """Find the native Osmium Tool, preferring the bundled Windows binary."""
    root = (
        Path(project_root).resolve()
        if project_root is not None
        else _PROJECT_ROOT
    )
    bundled = root / _BUNDLED_OSMIUM_RELATIVE
    if bundled.is_file():
        return str(bundled.resolve())
    return shutil.which("osmium")


def _normalize_timestamp(value: str) -> str:
    text = str(value).strip()
    if text.endswith("Z"):
        return text[:-1] + "+00:00"
    return text


def validate_preflight_snapshots(snapshots: Sequence[dict]) -> str:
    """Require selected regions to advertise one snapshot before download."""
    if not snapshots:
        raise ValueError("no multi-country snapshot metadata supplied")

    timestamps: list[str] = []
    labels: list[str] = []
    for snapshot in snapshots:
        timestamp = str(snapshot.get("timestamp") or "").strip()
        md5 = str(snapshot.get("md5") or "").strip().lower()
        name = str(snapshot.get("name") or snapshot.get("id") or "region")
        if not timestamp:
            raise ValueError(
                f"multi-country preflight failed before download: {name} "
                "has no Geofabrik replication timestamp"
            )
        if len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5):
            raise ValueError(
                f"multi-country preflight failed before download: {name} "
                "has no valid Geofabrik PBF MD5"
            )
        timestamps.append(_normalize_timestamp(timestamp))
        labels.append(f"{name}={timestamp}")

    unique = set(timestamps)
    if len(unique) != 1:
        raise ValueError(
            "multi-country preflight failed before download: selected regions "
            "are currently from different Geofabrik snapshots ("
            + ", ".join(labels)
            + ")"
        )

    return str(snapshots[0]["timestamp"])


def preflight_regions(regions: Sequence[dict]) -> tuple[list[dict], str | None]:
    """Pin current sizes, MD5s and replication state before downloading."""
    snapshots: list[dict] = []
    multi = len(regions) > 1

    for region in regions:
        size = geofabrik.remote_size(region["pbf"])
        expected_md5 = geofabrik.fetch_expected_md5(region["pbf"])
        timestamp: str | None = None
        sequence: str | None = None

        updates_url = region.get("updates")
        if updates_url:
            state = geofabrik.fetch_update_state(str(updates_url))
            timestamp = state.get("timestamp")
            sequence = state.get("sequenceNumber")
        elif multi:
            raise ValueError(
                "multi-country preflight failed before download: "
                f"{region['name']} has no Geofabrik updates/state.txt URL"
            )

        snapshots.append(
            {
                "id": str(region["id"]),
                "name": str(region["name"]),
                "size": size,
                "timestamp": timestamp,
                "sequence": sequence,
                "md5": expected_md5,
            }
        )

    shared_timestamp = (
        validate_preflight_snapshots(snapshots)
        if multi
        else (
            str(snapshots[0]["timestamp"])
            if snapshots and snapshots[0].get("timestamp")
            else None
        )
    )
    return snapshots, shared_timestamp


def _object_kind(obj: Any) -> str:
    if bool(obj.is_node()):
        return "node"
    if bool(obj.is_way()):
        return "way"
    if bool(obj.is_relation()):
        return "relation"
    raise ValueError(f"unsupported OSM object in merge stream: {obj!r}")


def _id_order(osm_id: int) -> tuple[int, int]:
    value = int(osm_id)
    if value < 0:
        return (0, abs(value))
    return (1, value)


def _object_key(obj: Any) -> tuple[int, int, int]:
    kind = _object_kind(obj)
    id_group, id_value = _id_order(int(obj.id))
    return (_TYPE_ORDER[kind], id_group, id_value)


def _object_version(obj: Any) -> int:
    try:
        return int(obj.version)
    except (AttributeError, TypeError, ValueError):
        return 0


def merge_sorted_streams(
    streams: Sequence[Iterator[Any]],
    writer: Any,
    *,
    progress_every: int = 0,
) -> dict[str, int]:
    """Merge sorted non-history OSM streams with bounded Python memory."""
    heap: list[tuple[tuple[int, int, int], int, Any]] = []
    last_key: list[tuple[int, int, int] | None] = [None] * len(streams)
    input_objects = 0
    output_objects = 0
    duplicates_removed = 0

    def push_next(source_index: int) -> None:
        nonlocal input_objects
        try:
            obj = next(streams[source_index])
        except StopIteration:
            return

        key = _object_key(obj)
        previous = last_key[source_index]
        if previous is not None and key <= previous:
            relation = (
                "duplicate type/ID"
                if key == previous
                else "out-of-order type/ID"
            )
            raise ValueError(
                f"OSM input stream {source_index + 1} is not sorted as a "
                f"normal snapshot ({relation}: previous={previous}, "
                f"current={key})"
            )

        last_key[source_index] = key
        input_objects += 1
        heapq.heappush(heap, (key, source_index, obj))

    for source_index in range(len(streams)):
        push_next(source_index)

    while heap:
        key, source_index, obj = heapq.heappop(heap)
        candidates: list[tuple[int, Any]] = [(source_index, obj)]

        while heap and heap[0][0] == key:
            _same_key, other_source, other_obj = heapq.heappop(heap)
            candidates.append((other_source, other_obj))

        _best_source, best_obj = max(
            candidates,
            key=lambda item: (_object_version(item[1]), -item[0]),
        )
        writer.add(best_obj)
        output_objects += 1
        duplicates_removed += len(candidates) - 1

        for consumed_source, _consumed_obj in candidates:
            push_next(consumed_source)

        if progress_every > 0 and output_objects % progress_every == 0:
            print(
                f"[merge] output={output_objects:,} "
                f"input={input_objects:,} "
                f"duplicates={duplicates_removed:,}"
            )

    return {
        "input_objects": input_objects,
        "output_objects": output_objects,
        "duplicates_removed": duplicates_removed,
    }


def _require_osmium_module():
    try:
        import osmium  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "Multi-country building requires pyosmium. Install project dependencies with:\n"
            "  python -m pip install -r requirements-release.txt"
        ) from exc
    return osmium


def _replication_signature(
    osmium: Any,
    path: Path,
) -> SnapshotSignature | None:
    try:
        header = osmium.replication.get_replication_header(str(path))
    except Exception:
        return None

    sequence = getattr(header, "sequence", None)
    timestamp = getattr(header, "timestamp", None)
    if sequence is None and timestamp is None:
        return None

    if timestamp is None:
        timestamp_text = None
    elif hasattr(timestamp, "isoformat"):
        timestamp_text = timestamp.isoformat()
    else:
        timestamp_text = str(timestamp)

    return (
        str(sequence) if sequence is not None else None,
        timestamp_text,
    )


def validate_snapshot_signatures(
    signatures: Sequence[SnapshotSignature | None],
    *,
    expected_timestamp: str | None = None,
) -> str:
    """Validate PBF header timestamps, using preflight metadata when supplied.

    Geofabrik PBFs do not universally carry replication metadata. When the
    builder has already pinned and validated the public ``updates/state.txt``
    timestamp before download, missing PBF header metadata is acceptable. Any
    header timestamp that is present must still agree with the pinned snapshot.
    """
    if expected_timestamp is not None:
        expected = _normalize_timestamp(expected_timestamp)
        for signature in signatures:
            if signature is None or signature[1] is None:
                continue
            actual = _normalize_timestamp(str(signature[1]))
            if actual != expected:
                raise ValueError(
                    "downloaded PBF header disagrees with the Geofabrik "
                    f"preflight snapshot: expected {expected_timestamp}, "
                    f"found {signature[1]}"
                )
        return expected_timestamp

    if not signatures:
        raise ValueError("no replication signatures supplied")

    timestamps: list[str] = []
    for signature in signatures:
        if signature is None or signature[1] is None:
            raise ValueError(
                "multi-country snapshot consistency could not be verified: "
                "at least one source PBF has no replication timestamp"
            )
        timestamps.append(_normalize_timestamp(str(signature[1])))

    unique = set(timestamps)
    if len(unique) != 1:
        raise ValueError(
            "selected source PBFs are from different replication snapshots: "
            + ", ".join(sorted(unique))
        )

    return str(signatures[0][1])


def _merge_with_osmium_cli(
    paths: Sequence[Path],
    output: Path,
    executable: str,
) -> dict[str, Any]:
    command = [
        executable,
        "merge",
        "-O",
        "-o",
        str(output),
        *[str(path) for path in paths],
    ]
    print("[merge] fast engine: " + " ".join(command))
    subprocess.run(command, check=True)

    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError(
            f"osmium merge did not create a valid output file: {output}"
        )

    return {
        "engine": "osmium",
        "input_objects": None,
        "output_objects": None,
        "duplicates_removed": None,
    }


def _merge_with_python(
    osmium: Any,
    paths: Sequence[Path],
    output: Path,
) -> dict[str, Any]:
    print(
        "[merge] portable engine: pyosmium streaming k-way merge; "
        "bounded Python memory, slower than osmium-tool"
    )

    processors = [osmium.FileProcessor(str(path)) for path in paths]
    streams = [iter(processor) for processor in processors]

    with osmium.SimpleWriter(str(output), overwrite=True) as writer:
        stats = merge_sorted_streams(
            streams,
            writer,
            progress_every=2_000_000,
        )

    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError(
            f"pyosmium merge did not create a valid output file: {output}"
        )

    return {"engine": "python", **stats}


def merge_pbfs(
    paths: Sequence[Path],
    output: Path,
    *,
    engine: str = "auto",
    expected_snapshot_timestamp: str | None = None,
    require_snapshot_consistency: bool = True,
) -> dict[str, Any]:
    """Merge country PBFs into one snapshot for the validated importer."""
    inputs = [Path(path).resolve() for path in paths]
    if not inputs:
        raise ValueError("no PBF inputs supplied")

    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if len(inputs) == 1:
        if output.exists():
            output.unlink()
        shutil.copy2(inputs[0], output)
        return {
            "engine": "copy",
            "source_count": 1,
            "source_bytes": inputs[0].stat().st_size,
            "output_bytes": output.stat().st_size,
            "input_objects": None,
            "output_objects": None,
            "duplicates_removed": 0,
            "same_replication_snapshot": True,
            "replication_timestamp": expected_snapshot_timestamp,
            "replication_sequences": [],
            "snapshot_verification": (
                "geofabrik-update-state"
                if expected_snapshot_timestamp
                else "none"
            ),
        }

    if engine not in {"auto", "osmium", "python"}:
        raise ValueError(f"unknown merge engine: {engine}")

    osmium = _require_osmium_module()
    signatures = [_replication_signature(osmium, path) for path in inputs]
    if require_snapshot_consistency:
        replication_timestamp = validate_snapshot_signatures(
            signatures,
            expected_timestamp=expected_snapshot_timestamp,
        )
        same_replication_snapshot: bool | None = True
        snapshot_verification = (
            "geofabrik-update-state"
            if expected_snapshot_timestamp is not None
            else "pbf-header"
        )
    else:
        header_timestamps = [
            signature[1] if signature is not None else None
            for signature in signatures
        ]
        if all(timestamp is not None for timestamp in header_timestamps):
            normalized = {
                _normalize_timestamp(str(timestamp))
                for timestamp in header_timestamps
                if timestamp is not None
            }
            if len(normalized) == 1:
                same_replication_snapshot = True
                replication_timestamp = str(header_timestamps[0])
                snapshot_verification = "pbf-header"
            else:
                same_replication_snapshot = False
                replication_timestamp = None
                snapshot_verification = "not-enforced"
                print(
                    "[merge] Local PBF replication timestamps differ; "
                    "continuing because snapshot consistency is not required "
                    "for local-file builds."
                )
        else:
            same_replication_snapshot = None
            replication_timestamp = None
            snapshot_verification = "not-enforced"
            print(
                "[merge] Local PBF replication timestamps are unavailable; "
                "continuing without snapshot verification."
            )

    sequences = [
        signature[0] if signature is not None else None
        for signature in signatures
    ]

    executable = find_osmium_executable()
    chosen = engine
    if chosen == "auto":
        chosen = "osmium" if executable else "python"

    if output.exists():
        output.unlink()

    if chosen == "osmium":
        if not executable:
            bundled = _PROJECT_ROOT / _BUNDLED_OSMIUM_RELATIVE
            raise SystemExit(
                "--merge-engine osmium requested but osmium-tool was not "
                f"found at {bundled} or on PATH. Use --merge-engine python "
                "or install/bundle osmium-tool."
            )
        print(f"[merge] osmium-tool executable: {executable}")
        stats = _merge_with_osmium_cli(inputs, output, executable)
    else:
        stats = _merge_with_python(osmium, inputs, output)

    stats["source_count"] = len(inputs)
    stats["source_bytes"] = sum(path.stat().st_size for path in inputs)
    stats["output_bytes"] = output.stat().st_size
    stats["same_replication_snapshot"] = same_replication_snapshot
    stats["replication_timestamp"] = replication_timestamp
    stats["replication_sequences"] = sequences
    stats["snapshot_verification"] = snapshot_verification
    return stats


def _download_destination(region: dict, out_dir: Path) -> Path:
    return geofabrik._destination_for(region, out_dir)


def ensure_region_download(
    region: dict,
    out_dir: Path,
    size: int | None,
    *,
    expected_md5: str | None = None,
) -> Path:
    """Download/resume one region against a pinned Geofabrik MD5."""
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = _download_destination(region, out_dir)
    expected = expected_md5 or geofabrik.fetch_expected_md5(region["pbf"])

    if destination.is_file() and (
        size is None or destination.stat().st_size == size
    ):
        print(f"[download] checking existing {destination.name}")
        actual = geofabrik.file_md5(destination)
        if actual.lower() == expected.lower():
            geofabrik.clear_download_resume_state(destination)
            print(f"[download] reuse verified {destination}")
            return destination

        print(f"[download] checksum mismatch; restarting {destination.name}")
        destination.unlink(missing_ok=True)
        geofabrik.clear_download_resume_state(destination)

    print(f"[download] {region['name']} -> {destination}")
    geofabrik.download_file(
        region["pbf"],
        destination,
        size,
        resume_identity=expected,
    )

    actual = geofabrik.file_md5(destination)
    if actual.lower() != expected.lower():
        raise IOError(
            f"MD5 verification FAILED for {destination.name}: "
            f"expected {expected}, got {actual}"
        )

    geofabrik.clear_download_resume_state(destination)
    print(f"[download] MD5 OK {destination.name}: {actual}")
    return destination


def print_plan(
    regions: Sequence[dict],
    sizes: Sequence[int | None],
) -> None:
    print("Selected Geofabrik regions:")
    known_total = 0
    unknown = 0

    for region, size in zip(regions, sizes):
        if size is None:
            unknown += 1
        else:
            known_total += size

        iso = ",".join(region.get("iso2") or []) or "-"
        print(
            f"  {region['name']:<28} {iso:<8} "
            f"{geofabrik.human_bytes(size):>12}  {region['id']}"
        )

    suffix = f" + {unknown} unknown" if unknown else ""
    print(
        f"PBF download total: "
        f"{geofabrik.human_bytes(known_total)}{suffix}"
    )
    print(
        "Temporary merged-PBF workspace is approximately up to the "
        "combined PBF size; final SQLite size remains region-dependent "
        "and is reported after import."
    )


def _check_merge_workspace(
    paths: Sequence[Path],
    directory: Path,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    needed = sum(path.stat().st_size for path in paths)
    usage = shutil.disk_usage(directory)

    if usage.free < needed:
        raise SystemExit(
            "Not enough free disk space for the temporary merged PBF: "
            f"need about {geofabrik.human_bytes(needed)}, "
            f"have {geofabrik.human_bytes(usage.free)}. "
            "This does not include space required by the SQLite import."
        )


def write_bundle_metadata(
    db_path: Path,
    regions: Sequence[dict],
    source_paths: Sequence[Path],
    merge_stats: dict[str, Any],
    merged_pbf: Path,
    *,
    merged_pbf_retained: bool,
    mission_audit: MissionAuditResult | None = None,
) -> None:
    provenance = {
        "kind": "multi-country" if len(regions) > 1 else "single-country",
        "regions": [
            {
                "id": str(region["id"]),
                "name": str(region["name"]),
                "iso2": list(region.get("iso2") or []),
                "pbf_url": str(region["pbf"]),
                "local_pbf": str(path.resolve()),
                "size_bytes": path.stat().st_size,
            }
            for region, path in zip(regions, source_paths)
        ],
        "merge": merge_stats,
        "merged_pbf": str(merged_pbf.resolve()),
        "merged_pbf_retained": bool(merged_pbf_retained),
    }

    values = {
        "build_kind": provenance["kind"],
        "source_region_count": str(len(regions)),
        "source_regions": json.dumps(
            provenance["regions"],
            ensure_ascii=False,
            sort_keys=True,
        ),
        "multi_build": json.dumps(
            provenance,
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    values.update(_mission_audit_metadata(mission_audit))

    replication_timestamp = merge_stats.get("replication_timestamp")
    if replication_timestamp:
        values["osm_timestamp"] = str(replication_timestamp)

    con = sqlite3.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            values.items(),
        )
        check = con.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise RuntimeError(
                "database quick_check failed after provenance update: "
                f"{check}"
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _default_db_path(regions: Sequence[dict]) -> Path:
    return Path("data") / f"{bundle_slug(regions)}.sqlite3"


def command_plan(args: argparse.Namespace) -> int:
    mission_audit = _run_mission_audit_preflight(
        getattr(args, "packages_root", None),
        excluded_files=getattr(args, "exclude_mission_file", ()),
    )
    index = geofabrik.fetch_index()
    regions = resolve_regions(index, args.regions)
    snapshots, shared_timestamp = preflight_regions(regions)
    sizes = [snapshot["size"] for snapshot in snapshots]
    print_plan(regions, sizes)
    if len(regions) > 1:
        print(f"Preflight snapshot: {shared_timestamp} [OK]")
    if mission_audit is not None:
        print(f"Mission profile: {mission_audit.mission_profile_hash} [OK]")
    print(f"Default database: {_default_db_path(regions)}")
    return 0


def command_build(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    mission_audit = _run_mission_audit_preflight(
        getattr(args, "packages_root", None),
        excluded_files=getattr(args, "exclude_mission_file", ()),
    )
    index = geofabrik.fetch_index()
    regions = resolve_regions(index, args.regions)
    selected_country_iso2 = _selected_country_iso2(regions)

    # All network metadata needed to decide whether this bundle is valid is
    # fetched before the first PBF byte is downloaded.
    snapshots, shared_timestamp = preflight_regions(regions)
    sizes = [snapshot["size"] for snapshot in snapshots]
    print_plan(regions, sizes)
    if len(regions) > 1:
        print(
            "[preflight] shared Geofabrik snapshot: "
            f"{shared_timestamp} [OK]"
        )

    downloads_dir = Path(args.downloads).resolve()
    work_dir = Path(args.work_dir).resolve()
    db_path = (
        Path(args.db).resolve()
        if args.db
        else _default_db_path(regions).resolve()
    )

    source_paths = [
        ensure_region_download(
            region,
            downloads_dir,
            snapshot["size"],
            expected_md5=str(snapshot["md5"]),
        )
        for region, snapshot in zip(regions, snapshots)
    ]

    slug = bundle_slug(regions)
    merged_pbf = work_dir / f"{slug}.merged.osm.pbf"
    work_dir.mkdir(parents=True, exist_ok=True)

    if len(source_paths) > 1:
        _check_merge_workspace(source_paths, work_dir)
        print(
            f"[merge] sources={len(source_paths)} "
            f"output={merged_pbf}"
        )
        merge_stats = merge_pbfs(
            source_paths,
            merged_pbf,
            engine=args.merge_engine,
            expected_snapshot_timestamp=shared_timestamp,
        )
        print(
            "[merge] snapshot verification: Geofabrik update-state "
            f"{merge_stats['replication_timestamp']} [OK]"
        )
        import_source = merged_pbf
    else:
        import_source = source_paths[0]
        merged_pbf = import_source
        merge_stats = {
            "engine": "none",
            "source_count": 1,
            "source_bytes": import_source.stat().st_size,
            "output_bytes": import_source.stat().st_size,
            "input_objects": None,
            "output_objects": None,
            "duplicates_removed": 0,
            "same_replication_snapshot": True,
            "replication_timestamp": shared_timestamp,
            "replication_sequences": [snapshots[0].get("sequence")],
            "snapshot_verification": (
                "geofabrik-update-state"
                if shared_timestamp
                else "single-country"
            ),
        }

    print(f"[import] source={import_source}")
    print(f"[import] target={db_path}")
    import_pbf(
        import_source,
        db_path,
        replace=args.replace,
        location_cache=args.location_cache,
        keep_cache=args.keep_cache,
        progress_every=max(0, int(args.progress_every)),
        workers=args.workers,
        worker_batch_size=max(250, int(args.worker_batch_size)),
        selected_country_iso2=selected_country_iso2,
        sql_batch_rows=args.sql_batch_rows,
        writer_batch_size=args.writer_batch_size,
        sql_write_mode=args.sql_write_mode,
    )

    should_remove_merged = (
        len(source_paths) > 1 and not args.keep_merged_pbf
    )
    if should_remove_merged:
        try:
            merged_pbf.unlink()
            print(f"[cleanup] removed temporary merged PBF: {merged_pbf}")
        except OSError as exc:
            print(f"[cleanup] could not remove temporary merged PBF: {exc}")

    write_bundle_metadata(
        db_path,
        regions,
        source_paths,
        merge_stats,
        merged_pbf,
        merged_pbf_retained=merged_pbf.exists(),
        mission_audit=mission_audit,
    )

    elapsed = time.perf_counter() - started
    print()
    print("Multi-country build complete")
    print(
        "Regions: "
        + ", ".join(str(region["name"]) for region in regions)
    )
    print(f"Database: {db_path}")
    print(
        f"Database size: "
        f"{geofabrik.human_bytes(db_path.stat().st_size)}"
    )
    if mission_audit is not None:
        print(f"Mission profile: {mission_audit.mission_profile_hash}")
    print(f"Elapsed: {elapsed / 60.0:,.1f} min")
    return 0


def _add_region_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "regions",
        nargs="+",
        help=(
            "Geofabrik region names, IDs or ISO-2 codes, "
            "e.g. Finland Sweden Norway"
        ),
    )


def _add_mission_audit_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exclude-mission-file", action="append", default=[],
        help="Mission JSON path to skip for this build (repeat for multiple files).",
    )
    parser.add_argument(
        "--packages-root",
        help=(
            "MSFS Packages or Community root. When supplied, installed "
            "hpgmission JSON is audited before network/download work and the "
            "passing mission-profile fingerprint is stored in DB metadata."
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Plan and build one verified H145 Local OSM SQLite database "
            "from one or more Geofabrik country/region extracts"
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser(
        "plan",
        help=(
            "preflight selected regions, verify H145 mission coverage and "
            "snapshot compatibility, and show current PBF sizes"
        ),
    )
    _add_region_arguments(plan)
    _add_mission_audit_argument(plan)
    plan.set_defaults(func=command_plan)

    build = sub.add_parser(
        "build",
        help=(
            "preflight, download, merge, import, verify and atomically "
            "install a country bundle"
        ),
    )
    _add_region_arguments(build)
    _add_mission_audit_argument(build)
    build.add_argument(
        "--downloads",
        default="downloads",
        help="download directory",
    )
    build.add_argument(
        "--work-dir",
        default="downloads/.multibuild",
        help="temporary merged-PBF directory",
    )
    build.add_argument(
        "--db",
        help=(
            "output SQLite database; defaults to "
            "data/<regions>.sqlite3"
        ),
    )
    build.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing database after verification",
    )
    build.add_argument(
        "--merge-engine",
        choices=("auto", "osmium", "python"),
        default="auto",
        help=(
            "auto prefers tools/osmium/osmium.exe, then osmium-tool on "
            "PATH, otherwise uses the bounded-memory Python merge"
        ),
    )
    build.add_argument(
        "--keep-merged-pbf",
        action="store_true",
        help="keep the temporary merged PBF after a successful import",
    )
    build.add_argument(
        "--location-cache",
        help="pyosmium location storage override",
    )
    build.add_argument(
        "--keep-cache",
        action="store_true",
        help="keep the temporary importer node cache",
    )
    build.add_argument(
        "--progress-every",
        type=int,
        default=500_000,
    )
    build.add_argument(
        "--workers",
        default="auto",
        help="topology worker processes: auto (default, up to 8), 0 for serial, or an integer",
    )
    build.add_argument(
        "--worker-batch-size",
        type=int,
        default=4_000,
    )
    from .bulk_write import add_build_write_arguments
    add_build_write_arguments(build)
    build.set_defaults(func=command_build)

    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except NETWORK_ERRORS as exc:
        # The packaged dispatcher handles SystemExit here. Keep the same marker
        # as coverage/preflight failures so the GUI can show recovery advice.
        raise SystemExit(network_failure_message(exc)) from None
    except (
        ValueError,
        IOError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        raise SystemExit(f"Multi-country build failed: {exc}") from exc


if __name__ == "__main__":
    main()
