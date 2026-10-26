# Third-Party Notices

H145 Local OSM is licensed under GNU GPL version 3 only (`GPL-3.0-only`). Third-party software and data used by or distributed with the project remain subject to their own copyright and license terms. Nothing in the H145 Local OSM license replaces those terms.

The exact Python versions used for an official build are recorded in `requirements-release.txt` and the release asset `PYTHON-DEPENDENCIES.txt`. Native Osmium source versions are recorded in `tools/osmium/pinned-versions.env` and `OSMIUM-VERSIONS.txt`.

## Main application dependencies

| Component | Role | License / terms | Source |
| --- | --- | --- | --- |
| Qt for Python / PySide6 | Windows GUI and Qt runtime | GNU GPL v3 and/or GNU LGPL v3, depending on the component; commercial licensing is also offered by Qt | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Qt | GUI framework libraries distributed through PySide6 | GNU GPL v3 and/or GNU LGPL v3 for the open-source components used here; individual Qt modules and bundled third-party components may carry additional compatible notices | https://code.qt.io/qt/ |
| pyosmium (`osmium` Python package) | OSM PBF processing bindings | BSD 2-Clause | https://github.com/osmcode/pyosmium |
| CPython | Python runtime included by the packaged application | Python Software Foundation License and applicable historical Python license notices | https://github.com/python/cpython |
| PyInstaller | Application packaging tool | GNU GPL v2 with the PyInstaller bootloader/bundling exception; the exception permits distribution of applications produced by PyInstaller under their applicable application licenses | https://github.com/pyinstaller/pyinstaller |
| Pillow | Image/icon handling used by the release toolchain | MIT-CMU | https://github.com/python-pillow/Pillow |

## Native Osmium runtime

Official Windows releases bundle Osmium Tool as a separate native executable used by the application for optimized OSM processing.

| Component | License / terms | Source |
| --- | --- | --- |
| Osmium Tool | GNU GPL v3 | https://github.com/osmcode/osmium-tool |
| libosmium | Boost Software License 1.0 | https://github.com/osmcode/libosmium |
| protozero | BSD 2-Clause | https://github.com/mapbox/protozero |
| Boost.Program_options | Boost Software License 1.0 | https://github.com/boostorg/program_options |
| bzip2 / libbzip2 | bzip2 license | https://sourceware.org/bzip2/ |
| Expat | MIT | https://github.com/libexpat/libexpat |
| LZ4 library | BSD 2-Clause | https://github.com/lz4/lz4 |
| nlohmann/json | MIT | https://github.com/nlohmann/json |
| zlib | zlib License | https://github.com/madler/zlib |

The official build obtains the native dependencies through vcpkg. The license and copyright terms supplied by those upstream projects and packages continue to apply to the corresponding DLLs.

The Osmium Tool GPL license text is bundled with the native runtime as `OSMIUM-TOOL-LICENSE.txt`. The pinned upstream source versions used for an official build are part of the repository and release metadata so the corresponding source version can be identified from the release.

## Source availability

The preferred source for H145 Local OSM is the source tree at the Git tag corresponding to the release. GitHub also provides source archives for each release tag.

For the bundled native Osmium runtime, the exact Osmium Tool, libosmium, and protozero source revisions are defined in `tools/osmium/pinned-versions.env`. The official release records those versions in `OSMIUM-VERSIONS.txt`. Upstream source repositories are listed above.

If a redistributed third-party component requires corresponding source or additional notices, its own license controls those obligations. Redistributors are responsible for preserving those rights and notices when redistributing the component.

## OpenStreetMap data

OpenStreetMap data is not licensed under the H145 Local OSM GPL license.

OpenStreetMap data is © OpenStreetMap contributors and is made available under the Open Database License (ODbL) 1.0. H145 Local OSM can download regional OpenStreetMap extracts provided by Geofabrik or import local `.osm.pbf` files supplied by the user.

OpenStreetMap copyright and license information: https://www.openstreetmap.org/copyright

ODbL 1.0: https://opendatacommons.org/licenses/odbl/1-0/

Geofabrik download service: https://download.geofabrik.de/

Generated databases containing OpenStreetMap data remain subject to applicable ODbL requirements independently of the GPL license covering the application code.

## External products

Microsoft Flight Simulator, HPG Airbus H145, Random & Everywhere (R&E), and other compatible mission packages are external products or projects. They are not included in this repository or relicensed by H145 Local OSM. Integration occurs through configuration files and the local HTTP compatibility endpoint.

Third-party names and trademarks are used only to identify compatibility. H145 Local OSM is not affiliated with Microsoft, Asobo Studio, Hype Performance Group, the OpenStreetMap Foundation, or Geofabrik.
