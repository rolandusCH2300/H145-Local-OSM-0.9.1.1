# H145 Local OSM v0.9.1.1

H145 Local OSM provides a local OpenStreetMap/Overpass-compatible backend for HPG H145 and compatible mission packages, including Random & Everywhere (R&E).

## Hotfix
- Fixes unreadable mission files by giving an option to skip affected missions


## Highlights

- Fixed discovery-stage RAM saturation during large database builds.
- Updated database build schema.
- Optimized recursive queries, regex lookups, and exact coverage filtering.

## Estimated total improvements compared with v0.9:
- Database size: 26.5% smaller.
- Database build time: 29.3% shorter.
- p50 query latency: 40.394% lower.
- p95 query latency: 42.170% lower.
- Slowest query latency: 50.904% lower.
Runtime updates require no database rebuild.

## Installation

1. Download `H145-Local-OSM-portable.zip`.
2. Optionally verify it with `SHA256SUMS.txt` from the same release.
3. Extract the complete archive to a writable folder.
4. Run `H145-Local-OSM.exe`.

Keep the extracted directory together because the application depends on files in `_internal`.

## License

H145 Local OSM is distributed under `GPL-3.0-only`. Third-party components retain their own licenses.

OpenStreetMap data is © OpenStreetMap contributors and is available under the Open Database License (ODbL).
