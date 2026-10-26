from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
USER_AGENT = "H145-Local-OSM/0.5 (+local Windows companion)"


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
):
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    return urllib.request.Request(url, method=method, headers=merged)


def fetch_index() -> dict:
    print(
        "[Geofabrik] Fetching region catalogue "
        "(network timeout: 30 seconds per blocking operation)...",
        flush=True,
    )
    with urllib.request.urlopen(_request(INDEX_URL), timeout=30) as response:
        return json.load(response)


def region_records(index: dict) -> list[dict]:
    out = []
    for feature in index.get("features", []):
        props = feature.get("properties") or {}
        urls = props.get("urls") or {}
        pbf = urls.get("pbf")
        if not pbf:
            continue
        out.append(
            {
                "id": props.get("id") or "",
                "name": props.get("name") or "",
                "parent": props.get("parent"),
                "iso2": list(props.get("iso3166-1:alpha2") or []),
                "pbf": pbf,
                "updates": urls.get("updates"),
            }
        )
    return out


def resolve_region(index: dict, query: str) -> dict:
    q = query.strip().lower()
    if not q:
        raise ValueError("region query is empty")
    records = region_records(index)

    exact = [
        r
        for r in records
        if r["id"].lower() == q
        or r["name"].lower() == q
        or q.upper() in r["iso2"]
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(
            f"ambiguous exact region query {query!r}: "
            f"{[r['id'] for r in exact]}"
        )

    partial = [
        r
        for r in records
        if q in r["id"].lower() or q in r["name"].lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError(f"no Geofabrik region matches {query!r}")
    raise ValueError(
        f"ambiguous region query {query!r}: "
        f"{[r['id'] for r in partial[:20]]}"
    )


def parse_update_state(text: str) -> dict[str, str]:
    """Parse an Osmosis/Geofabrik replication ``state.txt`` payload."""
    state: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Osmosis state files escape the colons in ISO timestamps as ``\:``.
        state[key.strip()] = value.strip().replace("\\:", ":")
    return state


def fetch_update_state(updates_url: str) -> dict[str, str]:
    """Fetch the current replication state for a Geofabrik extract."""
    if not updates_url:
        raise ValueError("Geofabrik region does not expose an updates URL")
    url = updates_url.rstrip("/") + "/state.txt"
    with urllib.request.urlopen(_request(url), timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    state = parse_update_state(text)
    if not state.get("timestamp"):
        raise ValueError(f"Geofabrik update state has no timestamp: {url}")
    return state


def remote_size(url: str) -> int | None:
    try:
        with urllib.request.urlopen(
            _request(url, method="HEAD"),
            timeout=30,
        ) as response:
            value = response.headers.get("Content-Length")
            if value:
                return int(value)
    except Exception:
        pass
    try:
        with urllib.request.urlopen(
            _request(url, headers={"Range": "bytes=0-0"}),
            timeout=30,
        ) as response:
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                return int(content_range.rsplit("/", 1)[1])
            value = response.headers.get("Content-Length")
            return int(value) if value else None
    except Exception:
        return None


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    n = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024.0 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{value} B"


def fetch_expected_md5(pbf_url: str) -> str:
    with urllib.request.urlopen(
        _request(pbf_url + ".md5"),
        timeout=30,
    ) as response:
        text = response.read().decode("utf-8", errors="replace").strip()
    token = text.split()[0].lower() if text else ""
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError(f"unexpected MD5 response for {pbf_url}")
    return token


def file_md5(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _download_state_path(destination: Path) -> Path:
    return Path(str(destination) + ".download.json")


def _write_download_state(
    state_path: Path,
    *,
    url: str,
    expected_size: int | None,
    resume_identity: str,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": str(url),
        "expected_size": expected_size,
        "resume_identity": str(resume_identity),
    }
    tmp = Path(str(state_path) + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, state_path)


def _read_download_state(state_path: Path) -> dict | None:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def clear_download_resume_state(destination: Path) -> None:
    _download_state_path(destination).unlink(missing_ok=True)


def prepare_resumable_download(
    url: str,
    destination: Path,
    *,
    expected_size: int | None,
    resume_identity: str | None,
) -> int:
    """Return a safe resume offset, restarting stale/unknown partial files.

    A Geofabrik filename such as ``finland-latest.osm.pbf`` is reused across
    daily snapshots. File size alone therefore cannot prove that an existing
    prefix belongs to the currently published object. A partial file is only
    resumed when its sidecar records the same URL, expected size and immutable
    snapshot identity (the published MD5 in current callers).
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    state_path = _download_state_path(destination)
    existing = destination.stat().st_size if destination.exists() else 0

    current_state = {
        "url": str(url),
        "expected_size": expected_size,
        "resume_identity": (
            str(resume_identity) if resume_identity is not None else None
        ),
    }

    restart_reason: str | None = None
    if existing:
        if expected_size is not None and existing > expected_size:
            restart_reason = "existing file is larger than the current snapshot"
        elif resume_identity is None:
            restart_reason = "no snapshot identity is available"
        else:
            saved = _read_download_state(state_path)
            if saved is None:
                restart_reason = "legacy partial has no snapshot identity"
            elif saved != current_state:
                restart_reason = "partial belongs to a different snapshot"

    if restart_reason is not None:
        print(
            f"Existing partial cannot be safely resumed ({restart_reason}); "
            "restarting from zero."
        )
        destination.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        existing = 0

    if resume_identity is not None:
        _write_download_state(
            state_path,
            url=url,
            expected_size=expected_size,
            resume_identity=str(resume_identity),
        )
    elif existing == 0:
        state_path.unlink(missing_ok=True)

    return existing


def download_file(
    url: str,
    destination: Path,
    expected_size: int | None = None,
    *,
    resume_identity: str | None = None,
) -> None:
    destination = Path(destination)
    if resume_identity is None:
        resume_identity = fetch_expected_md5(url)

    existing = prepare_resumable_download(
        url,
        destination,
        expected_size=expected_size,
        resume_identity=resume_identity,
    )

    if expected_size is not None:
        remaining = max(0, expected_size - existing)
        usage = shutil.disk_usage(destination.parent)
        if usage.free < remaining:
            raise IOError(
                "not enough free disk space after resume validation: "
                f"need {human_bytes(remaining)}, have {human_bytes(usage.free)}"
            )

    headers: dict[str, str] = {}
    if existing:
        headers["Range"] = f"bytes={existing}-"
        print(f"Resuming at {human_bytes(existing)}")

    req = _request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        status = getattr(response, "status", 200)
        if existing:
            content_range = response.headers.get("Content-Range", "")
            expected_prefix = f"bytes {existing}-"
            if status != 206 or not content_range.startswith(expected_prefix):
                print(
                    "Server did not honor the requested byte range exactly; "
                    "restarting download from zero."
                )
                destination.unlink(missing_ok=True)
                clear_download_resume_state(destination)
                response.close()
                return download_file(
                    url,
                    destination,
                    expected_size,
                    resume_identity=resume_identity,
                )

        mode = "ab" if existing else "wb"
        total = expected_size
        if total is None:
            length = response.headers.get("Content-Length")
            if length:
                total = existing + int(length)

        written = existing
        next_report = written + 32 * 1024 * 1024
        with destination.open(mode) as out:
            while True:
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if written >= next_report:
                    if total:
                        pct = written * 100.0 / total
                        print(
                            f"Downloaded {human_bytes(written)} / "
                            f"{human_bytes(total)} ({pct:.1f}%)"
                        )
                    else:
                        print(f"Downloaded {human_bytes(written)}")
                    next_report = written + 32 * 1024 * 1024

    if expected_size is not None and destination.stat().st_size != expected_size:
        raise IOError(
            f"download size mismatch: got {destination.stat().st_size}, "
            f"expected {expected_size} bytes"
        )


def _destination_for(region: dict, out_dir: Path) -> Path:
    basename = urllib.request.url2pathname(region["pbf"].split("/")[-1])
    return out_dir / basename


def command_info(args) -> int:
    index = fetch_index()
    region = resolve_region(index, args.region)
    size = remote_size(region["pbf"])
    print(f"Region: {region['name']} ({region['id']})")
    print(f"ISO:    {', '.join(region['iso2']) if region['iso2'] else '-'}")
    print(f"PBF download size: {human_bytes(size)}")
    print(
        "Installed SQLite size: unknown until this profile has been "
        "benchmarked on an imported PBF."
    )
    return 0


def command_list(args) -> int:
    index = fetch_index()
    q = (args.search or "").lower()
    records = region_records(index)
    if q:
        records = [
            r
            for r in records
            if q in r["name"].lower()
            or q in r["id"].lower()
            or q.upper() in r["iso2"]
        ]
    for r in records[: args.limit]:
        iso = ",".join(r["iso2"]) or "-"
        print(f"{r['id']:<35} {iso:<8} {r['name']}")
    return 0


def command_download(args) -> int:
    index = fetch_index()
    region = resolve_region(index, args.region)
    size = remote_size(region["pbf"])
    expected = fetch_expected_md5(region["pbf"])
    out_dir = Path(args.out).resolve()
    destination = _destination_for(region, out_dir)

    usage = shutil.disk_usage(out_dir if out_dir.exists() else out_dir.parent)
    if size is not None:
        remaining = max(
            0,
            size - (destination.stat().st_size if destination.exists() else 0),
        )
        if usage.free < remaining:
            raise SystemExit(
                f"Not enough free disk space: need {human_bytes(remaining)}, "
                f"have {human_bytes(usage.free)}"
            )

    print(f"Region: {region['name']} ({region['id']})")
    print(f"PBF size: {human_bytes(size)}")
    print(f"Destination: {destination}")

    if destination.is_file() and (
        size is None or destination.stat().st_size == size
    ):
        actual = file_md5(destination)
        if actual.lower() == expected.lower():
            clear_download_resume_state(destination)
            print(f"MD5 OK: {actual}")
            print(destination)
            return 0
        print("Existing complete file failed current MD5; restarting download.")
        destination.unlink(missing_ok=True)
        clear_download_resume_state(destination)

    download_file(
        region["pbf"],
        destination,
        size,
        resume_identity=expected,
    )

    print("Verifying Geofabrik MD5...")
    actual = file_md5(destination)
    if actual.lower() != expected.lower():
        raise SystemExit(
            f"MD5 verification FAILED: expected {expected}, got {actual}"
        )
    clear_download_resume_state(destination)
    print(f"MD5 OK: {actual}")
    print(destination)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Geofabrik country/region discovery and PBF downloader"
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list Geofabrik regions")
    p_list.add_argument("--search")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=command_list)

    p_info = sub.add_parser("info", help="show current PBF size for a region")
    p_info.add_argument("region")
    p_info.set_defaults(func=command_info)

    p_dl = sub.add_parser(
        "download",
        help="download/resume and verify a region PBF",
    )
    p_dl.add_argument("region")
    p_dl.add_argument("--out", default="downloads")
    p_dl.set_defaults(func=command_download)

    args = p.parse_args()
    try:
        raise SystemExit(args.func(args))
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        IOError,
    ) as exc:
        raise SystemExit(f"Geofabrik operation failed: {exc}") from exc


if __name__ == "__main__":
    main()
