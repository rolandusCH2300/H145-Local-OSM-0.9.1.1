from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .. import geofabrik
from ..bulk_write import DEFAULT_WRITE_OPTIONS
from ..importer import import_pbf
from ..mission_audit import audit_occurrences, scan_mission_queries
from ..multibuild import _mission_audit_metadata, merge_pbfs

LOCAL_OVERPASS_NAME = "Local OSM"
OFFICIAL_OVERPASS_NAME = "Overpass DE"
LOCAL_OVERPASS_URL = "http://127.0.0.1:8765/api/interpreter?data="
OFFICIAL_OVERPASS_URL = "https://overpass-api.de/api/interpreter?data="
LEGACY_HPG_BACKUP_SUFFIX = ".relocalosm.backup"
CONFIG_FILENAME = "config.json"


@dataclass(frozen=True, slots=True)
class MissionEntry:
    package: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class HPGConnectionStatus:
    mission_system: Path | None
    current_url: str | None
    configured: bool
    backup_available: bool
    detail: str = ""


class PortableConfigStore:
    """Small portable config stored beside the application executable."""

    def __init__(self, root: Path | None = None):
        self.root = (root or application_root()).resolve()
        self.path = self.root / CONFIG_FILENAME

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def update(self, **changes) -> dict:
        value = self.load()
        value.update(changes)
        self.save(value)
        return value


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_directory(root: Path | None = None) -> Path:
    return (root or application_root()) / "data"


def reports_directory(root: Path | None = None) -> Path:
    return (root or application_root()) / "reports" / "gui"


def discover_databases(root: Path | None = None) -> list[Path]:
    directory = data_directory(root)
    if not directory.is_dir():
        return []
    values = [path.resolve() for path in directory.glob("*.sqlite3") if path.is_file()]
    return sorted(values, key=lambda path: (path.stat().st_mtime_ns, path.name.lower()), reverse=True)


def choose_default_database(databases: Sequence[Path], last_database: str | None) -> Path | None:
    if not databases:
        return None
    if last_database:
        wanted = Path(last_database).name.lower()
        for path in databases:
            if path.name.lower() == wanted:
                return path
    return databases[0]


def remember_database(store: PortableConfigStore, database: Path) -> None:
    store.update(last_database=Path(database).name)


def _community_from_usercfg(path: Path) -> Path | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    match = re.search(r'^\s*InstalledPackagesPath\s+"([^"]+)"', text, re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    packages = Path(os.path.expandvars(match.group(1))).expanduser()
    community = packages / "Community"
    return community.resolve() if community.is_dir() else None


def detect_community_folder(saved: str | Path | None = None) -> Path | None:
    """Best-effort detection. Missing/invalid locations are intentionally silent."""
    if saved:
        candidate = Path(saved).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    usercfg_candidates: list[Path] = []
    direct_candidates: list[Path] = []

    if local:
        limit = Path(local) / "Packages" / "Microsoft.Limitless_8wekyb3d8bbwe" / "LocalCache"
        usercfg_candidates.append(limit / "UserCfg.opt")
        direct_candidates.append(limit / "Packages" / "Community")
    if roaming:
        roaming_path = Path(roaming)
        usercfg_candidates.extend(
            [
                roaming_path / "Microsoft Flight Simulator 2024" / "UserCfg.opt",
                roaming_path / "Microsoft Flight Simulator" / "UserCfg.opt",
            ]
        )

    for path in usercfg_candidates:
        community = _community_from_usercfg(path)
        if community is not None:
            return community
    for path in direct_candidates:
        if path.is_dir():
            return path.resolve()
    return None


def discover_missions(community: Path | None) -> list[MissionEntry]:
    if community is None or not Path(community).is_dir():
        return []
    root = Path(community).resolve()
    entries: list[MissionEntry] = []
    try:
        paths = root.rglob("*.json")
        for path in paths:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parent_parts = [part.lower() for part in rel.parts[:-1]]
            if "hpgmission" not in parent_parts:
                continue
            package = rel.parts[0] if rel.parts else path.parent.name
            entries.append(MissionEntry(package=package, relative_path=str(rel)))
    except OSError:
        return []
    return sorted(entries, key=lambda item: (item.package.lower(), item.relative_path.lower()))


def fetch_geofabrik_catalog() -> list[dict]:
    """Fetch the downloadable catalog once. Callers own failure presentation."""
    return geofabrik.region_records(geofabrik.fetch_index())


def _hpg_candidate_score(path: Path) -> tuple[int, int]:
    lower_parts = [part.lower() for part in path.parts]
    score = 10 if path.name.lower() == "missionsystem.json" else 0
    if "hpg-airbus-h145" in lower_parts:
        score += 100
    if "hpg-airbus-h145-ap" in lower_parts:
        score -= 50
    return score, -len(str(path))


def find_hpg_mission_system(community: Path | None) -> Path | None:
    if community is None or not Path(community).is_dir():
        return None
    try:
        candidates = [
            path for path in Path(community).rglob("MissionSystem.json") if path.is_file()
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=_hpg_candidate_score).resolve()


def _read_hpg_overpass_entry(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    values = payload.get("OverpassApiUrls")
    if not isinstance(values, list):
        raise ValueError("MissionSystem.json does not contain OverpassApiUrls")
    names = {OFFICIAL_OVERPASS_NAME, LOCAL_OVERPASS_NAME}
    matches = [
        item
        for item in values
        if isinstance(item, dict) and item.get("name") in names
    ]
    if len(matches) != 1:
        raise ValueError(
            "expected exactly one Overpass DE/Local OSM entry, "
            f"found {len(matches)}"
        )
    return str(matches[0].get("name") or ""), str(matches[0].get("url") or "")


def _read_overpass_de(path: Path) -> str:
    """Compatibility helper returning the active HPG Overpass-slot URL."""
    return _read_hpg_overpass_entry(path)[1]


def hpg_connection_status(community: Path | None) -> HPGConnectionStatus:
    mission_system = find_hpg_mission_system(community)
    if mission_system is None:
        return HPGConnectionStatus(None, None, False, False, "MissionSystem.json not found")
    backup = Path(str(mission_system) + LEGACY_HPG_BACKUP_SUFFIX)
    try:
        current_name, current = _read_hpg_overpass_entry(mission_system)
    except Exception as exc:
        return HPGConnectionStatus(
            mission_system,
            None,
            False,
            backup.is_file(),
            f"{type(exc).__name__}: {exc}",
        )
    return HPGConnectionStatus(
        mission_system,
        current,
        current_name == LOCAL_OVERPASS_NAME and current == LOCAL_OVERPASS_URL,
        backup.is_file(),
        "",
    )


def configure_hpg_connection(community: Path, *, allow_nonstandard: bool = False) -> HPGConnectionStatus:
    status = hpg_connection_status(community)
    if status.mission_system is None:
        raise FileNotFoundError("HPG MissionSystem.json was not found")
    if status.current_url is None:
        raise ValueError(status.detail or "could not read HPG Overpass endpoint")

    path = status.mission_system
    current_name, current_url = _read_hpg_overpass_entry(path)
    if current_name == LOCAL_OVERPASS_NAME and current_url == LOCAL_OVERPASS_URL:
        return status

    legacy_local = (
        current_name == OFFICIAL_OVERPASS_NAME and current_url == LOCAL_OVERPASS_URL
    )
    if (
        current_url != OFFICIAL_OVERPASS_URL
        and not legacy_local
        and not allow_nonstandard
    ):
        raise ValueError(
            f"{current_name} currently uses a non-standard URL: {current_url}"
        )

    raw = path.read_text(encoding="utf-8-sig")
    updated = raw

    if current_name != LOCAL_OVERPASS_NAME:
        current_name_token = json.dumps(current_name)
        local_name_token = json.dumps(LOCAL_OVERPASS_NAME)
        if updated.count(current_name_token) != 1:
            raise ValueError(
                "active Overpass provider name does not occur exactly once; no change made"
            )
        updated = updated.replace(current_name_token, local_name_token, 1)

    if current_url != LOCAL_OVERPASS_URL:
        if updated.count(current_url) != 1:
            raise ValueError(
                "active Overpass provider URL does not occur exactly once; no change made"
            )
        updated = updated.replace(current_url, LOCAL_OVERPASS_URL, 1)

    backup = Path(str(path) + LEGACY_HPG_BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)

    tmp = path.with_suffix(path.suffix + ".h145localosm.tmp")
    tmp.write_text(updated, encoding="utf-8")
    try:
        patched_name, patched_url = _read_hpg_overpass_entry(tmp)
        if (
            patched_name != LOCAL_OVERPASS_NAME
            or patched_url != LOCAL_OVERPASS_URL
        ):
            raise ValueError("patched MissionSystem.json verification failed")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return hpg_connection_status(community)


def restore_hpg_connection(community: Path) -> HPGConnectionStatus:
    status = hpg_connection_status(community)
    if status.mission_system is None:
        raise FileNotFoundError("HPG MissionSystem.json was not found")
    backup = Path(str(status.mission_system) + LEGACY_HPG_BACKUP_SUFFIX)
    if not backup.is_file():
        raise FileNotFoundError("no H145 Local OSM endpoint backup exists")
    _read_hpg_overpass_entry(backup)
    shutil.copy2(backup, status.mission_system)
    return hpg_connection_status(community)


def _internal_prefix() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-task"]
    return [sys.executable, "-m", "local_osm.gui", "--internal-task"]


def server_command(database: Path) -> list[str]:
    return _internal_prefix() + ["server", "--db", str(Path(database).resolve())]


def validation_command(database: Path, community: Path | None, root: Path | None = None) -> list[str]:
    command = _internal_prefix() + [
        "validate",
        "--db",
        str(Path(database).resolve()),
        "--output-dir",
        str(reports_directory(root) / "validation"),
    ]
    if community is not None:
        command.extend(["--packages-root", str(Path(community).resolve())])
    return command


def benchmark_command(database: Path, community: Path | None, root: Path | None = None) -> list[str]:
    command = _internal_prefix() + [
        "benchmark",
        "--db",
        str(Path(database).resolve()),
        "--repeat",
        "7",
        "--output-dir",
        str(reports_directory(root) / "benchmark"),
    ]
    if community is not None:
        command.extend(["--packages-root", str(Path(community).resolve())])
    return command


def api_database_name(regions: Sequence[dict]) -> str:
    from ..multibuild import bundle_slug

    return f"{bundle_slug(regions)}.sqlite3"


def local_database_name(paths: Sequence[Path]) -> str:
    if not paths:
        return "local.sqlite3"
    pieces = []
    for path in paths[:3]:
        stem = Path(path).name.lower()
        stem = re.sub(r"(?:-latest)?\.osm\.pbf$", "", stem)
        stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-") or "local"
        pieces.append(stem)
    suffix = "-more" if len(paths) > 3 else ""
    return "-".join(pieces)[:90].rstrip("-") + suffix + ".sqlite3"


def api_build_command(
    region_ids: Sequence[str],
    database: Path,
    community: Path | None,
    *,
    serial_topology: bool,
) -> list[str]:
    command = _internal_prefix() + ["build-api", *region_ids, "--db", str(Path(database).resolve()), "--replace"]
    command.extend(["--workers", "0" if serial_topology else "auto"])
    if community is not None:
        command.extend(["--packages-root", str(Path(community).resolve())])
    return command


def local_build_command(
    pbf_paths: Sequence[Path],
    database: Path,
    community: Path | None,
    *,
    serial_topology: bool,
) -> list[str]:
    command = _internal_prefix() + [
        "build-local",
        *[str(Path(path).resolve()) for path in pbf_paths],
        "--db",
        str(Path(database).resolve()),
        "--workers",
        "0" if serial_topology else "auto",
    ]
    if community is not None:
        command.extend(["--packages-root", str(Path(community).resolve())])
    return command


def _audit_for_local_build(community: Path | None, *, excluded_files: Sequence[str | Path] = ()):
    if community is None:
        return None
    scan = scan_mission_queries(community, excluded_files=excluded_files)
    for path in scan.excluded_files:
        print(f"[mission-audit] Skipping mission file for this build: {Path(scan.packages_root) / path}")
    result = audit_occurrences(scan)
    if not result.ok:
        problems = []
        if result.missing_requirements:
            problems.append(f"missing import requirements={len(result.missing_requirements)}")
        if result.parse_errors:
            problems.append(f"unsupported queries={len(result.parse_errors)}")
        if result.scan_errors:
            problems.append(f"mission JSON read errors={len(result.scan_errors)}")
            for item in result.scan_errors:
                print(f"[mission-audit] JSON read error: {Path(result.packages_root) / item.path}: {item.error}")
        raise ValueError("H145 mission audit failed before local import: " + ", ".join(problems))
    print(
        f"[mission-audit] queries={result.query_occurrences} unique={result.unique_queries} "
        f"uncovered={result.uncovered_branches} profile={result.mission_profile_hash} [OK]"
    )
    return result


def run_local_build(
    pbf_paths: Sequence[Path],
    database: Path,
    *,
    community: Path | None = None,
    excluded_mission_files: Sequence[str | Path] = (),
    workers: str = "auto",
    sql_batch_rows: int = DEFAULT_WRITE_OPTIONS.sql_rows,
    writer_batch_size: int = DEFAULT_WRITE_OPTIONS.writer_rows,
    sql_write_mode: str = DEFAULT_WRITE_OPTIONS.sql_write_mode,
    root: Path | None = None,
) -> Path:
    sources = [Path(path).resolve() for path in pbf_paths]
    if not sources:
        raise ValueError("select at least one local .osm.pbf file")
    for path in sources:
        if not path.is_file():
            raise FileNotFoundError(path)

    app_root = (root or application_root()).resolve()
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    audit = _audit_for_local_build(community, excluded_files=excluded_mission_files)

    merged: Path | None = None
    source = sources[0]
    merge_stats: dict = {
        "engine": "none",
        "source_count": 1,
        "same_replication_snapshot": True,
    }
    if len(sources) > 1:
        work = app_root / "downloads" / ".localbuild"
        work.mkdir(parents=True, exist_ok=True)
        merged = work / (database.stem + ".merged.osm.pbf")
        print(f"[merge] Combining {len(sources)} local PBF files...")
        print(f"[merge] Output: {merged}")
        merge_stats = merge_pbfs(
            sources,
            merged,
            engine="auto",
            require_snapshot_consistency=False,
        )
        print(f"[merge] Engine: {merge_stats.get('engine', 'unknown')}")
        print("[merge] Complete")
        source = merged
    else:
        print("[merge] Single local PBF selected; merge not required.")

    try:
        print(f"[import] Starting database import: {source}")
        import_pbf(
            source, database, replace=True, workers=workers,
            sql_batch_rows=sql_batch_rows, writer_batch_size=writer_batch_size,
            sql_write_mode=sql_write_mode,
        )
        metadata = {
            "build_kind": "local-pbf",
            "source_region_count": str(len(sources)),
            "source_regions": json.dumps(
                [
                    {
                        "id": path.stem,
                        "name": path.name,
                        "iso2": [],
                        "pbf_url": None,
                        "local_pbf": str(path),
                        "size_bytes": path.stat().st_size,
                    }
                    for path in sources
                ],
                ensure_ascii=False,
                sort_keys=True,
            ),
            "local_build": json.dumps(
                {
                    "sources": [str(path) for path in sources],
                    "merge": merge_stats,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        metadata.update(_mission_audit_metadata(audit))
        con = sqlite3.connect(database)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.executemany(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                metadata.items(),
            )
            con.commit()
        finally:
            con.close()
    finally:
        if merged is not None:
            merged.unlink(missing_ok=True)
    print(f"Local PBF build complete: {database}")
    return database
