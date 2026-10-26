# H145 Local OSM

H145 Local OSM is a Windows desktop application that provides a local OpenStreetMap/Overpass-compatible backend for HPG H145 and compatible mission packages, including Random & Everywhere (R&E).

It downloads or imports OpenStreetMap PBF data, builds a local SQLite database, configures supported HPG/R&E files, and serves supported mission queries from `127.0.0.1:8765`.

## Main features

- Windows desktop GUI for database building and server control.
- Geofabrik country and regional OSM downloads.
- Import of local `.osm.pbf` files for a fully offline workflow.
- Multi-region database builds.
- SQLite/RTree storage with query indexes and packed geometry/tag caches.
- Overpass-compatible handling for the HPG/R&E/other supported mission query patterns.
- HPG and R&E compatibility patching with backup and restore.
- Database validation and benchmarking.
- Bundled Osmium Tool runtime in official Windows releases.
- Portable configuration, databases, downloads, and reports.

## Requirements

- 64-bit Windows.
- Microsoft Flight Simulator 2024 or 2020
- HPG Airbus H145 + Action pack.
- Enough disk space for the selected OSM extracts and generated database.

## Installation

1. Download `H145-Local-OSM-portable.zip` from the latest GitHub Release.
2. Optionally verify it against `SHA256SUMS.txt` from the same release.
3. Extract the complete ZIP.
4. Run `H145-Local-OSM.exe`.

Keep the extracted directory together; the executable depends on files in `_internal`.

## Build a database

Open the **Build** page:

1. Choose **Download from Geofabrik API** or **Local .osm.pbf files**.
2. Select the regions or local files to import.
3. Select the MSFS Community folder if it is not detected automatically.
4. Enable **Limited-hardware topology (serial)** only when lower resource use is preferred over build speed.
5. Select **Build database**.

Databases are stored in the portable `data` directory. Geofabrik builds include region coverage metadata; arbitrary local PBF builds do not assume known geographic coverage.

Before a build, unreadable mission JSON files are listed in a popup with their full paths and errors. Choose **Cancel** or **Continue without these files** to skip only the listed files for this build. Skipped files stay unchanged on disk, and their mission queries are excluded from the coverage audit. The build log and database metadata record the exclusions; the remaining missions are still audited.

## Connect HPG and start the server

Open the **Server** page:

1. Select a database.
2. Run **Validate database** after building or replacing data.
3. Under **Compatibility patches**, use **Patch** for the HPG connection.
4. If a supported R&E version is installed, use **Patch** for the R&E missions.
5. Update mission index from H145 tools or the action center
6. Select **Start server**.

Local endpoint:

```text
http://127.0.0.1:8765/api/interpreter?data=
```

Health endpoint:

```text
http://127.0.0.1:8765/health
```

Compatibility patches create backups before modifying installed files. Use **Restore** to return to the saved original. Restart MSFS after changing a patch while the simulator is running.

## Validation and benchmark

**Validate database** checks the selected database and can audit discovered mission queries when a Community folder is available.

**Benchmark database** measures supported query execution. Stop the server before running a benchmark.

Reports are written to the portable `reports` directory.

## Network behavior

The query server listens only on `127.0.0.1` and is not exposed to the local network or internet. Runtime mission queries do not fall back to a public Overpass server.

Outbound access is used for Geofabrik metadata and OSM downloads. Local PBF builds do not require an OSM download.

## Build from source

Release dependencies are pinned in `requirements-release.txt`.

```powershell
python -m pip install -r requirements-release.txt
python -m unittest discover -s tests -v
.\tools\build_portable_gui.ps1
```

Official releases also build and verify the pinned native Osmium Tool runtime.

## Security

See `SECURITY.md` for the security model, vulnerability reporting, and release verification instructions. Do not report undisclosed vulnerabilities in a public issue.

## License

H145 Local OSM is licensed under **GPL-3.0-only**. See `LICENSE`.

Third-party components retain their own licenses. See `THIRD_PARTY_NOTICES.md`.

OpenStreetMap data is © OpenStreetMap contributors and is available under the Open Database License (ODbL). Geofabrik provides regional OpenStreetMap extracts.

Third-party product and project names are used only to identify compatibility. H145 Local OSM is not affiliated with Microsoft, Asobo Studio, Hype Performance Group, the OpenStreetMap Foundation, or Geofabrik.

Copyright © 2026 Roni Tuomi.
