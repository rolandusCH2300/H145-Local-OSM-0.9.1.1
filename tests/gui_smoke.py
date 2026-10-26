from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import local_osm.gui.app as base_gui
import local_osm.gui.enhanced_app_v2 as gui


def main() -> int:
    app = QApplication.instance() or QApplication([])
    with (
        tempfile.TemporaryDirectory() as tmp,
        mock.patch.object(base_gui, "application_root", return_value=Path(tmp)),
        mock.patch.object(base_gui, "detect_community_folder", return_value=None),
        mock.patch.object(base_gui.CatalogWorker, "start", lambda self: None),
        mock.patch.object(base_gui, "probe_local_server", lambda: False),
        mock.patch.object(gui.geofabrik, "remote_size", lambda _url: None),
    ):
        window = gui.MainWindow()
        try:
            window.show()
            app.processEvents()
            assert window.pages.count() == 2
            assert window.pages.currentWidget() is window.setup_page
            assert window.setup_nav.text() == "Build"
            assert window.server_nav.text() == "Server"
            assert window.setup_nav.isChecked()
            assert not window.server_nav.isChecked()
            assert window.build_button.text() == "Build database"
            assert window.validate_button.text() == "Validate database"
            assert window.benchmark_button.text() == "Benchmark database"
            assert window.start_button.text() == "Start server"
            assert window.stop_button.text() == "Stop server"
            assert window.community_button.text() == "No Community folder selected"
            assert "serial" in window.serial_checkbox.text().lower()
            assert "Estimated Local OSM DB" in window.api_size_label.text()
            # Layout requirements depend on Qt, fonts and display scaling.
            # Check that the actual controls fit, not an old pixel minimum.
            assert window.size().width() >= window.minimumSizeHint().width()
            assert window.size().height() >= window.minimumSizeHint().height()
            assert window.build_button.isVisible()
            assert window.rect().contains(
                window.build_button.mapTo(window, window.build_button.rect().bottomRight())
            )

            window.pages.setCurrentWidget(window.server_page)
            app.processEvents()
            assert window.start_button.isVisible()
            assert window.rect().contains(
                window.start_button.mapTo(window, window.start_button.rect().bottomRight())
            )
            assert window.server_nav.isChecked()
            assert not window.setup_nav.isChecked()
            assert window.server_nav.font().bold()
            window.pages.setCurrentWidget(window.setup_page)
            app.processEvents()
            assert window.setup_nav.isChecked()
            assert window.setup_nav.font().bold()

            window._catalog_loaded(
                [
                    {
                        "id": "europe",
                        "name": "Europe",
                        "parent": None,
                        "iso2": [],
                        "pbf": "https://example.invalid/europe.osm.pbf",
                    },
                    {
                        "id": "africa",
                        "name": "Africa",
                        "parent": None,
                        "iso2": [],
                        "pbf": "https://example.invalid/africa.osm.pbf",
                    },
                    {
                        "id": "antarctica",
                        "name": "Antarctica",
                        "parent": None,
                        "iso2": ["AQ"],
                        "pbf": "https://example.invalid/antarctica.osm.pbf",
                    },
                    {
                        "id": "asia",
                        "name": "Asia",
                        "parent": None,
                        "iso2": [],
                        "pbf": "https://example.invalid/asia.osm.pbf",
                    },
                    {
                        "id": "finland",
                        "name": "Finland",
                        "parent": "europe",
                        "iso2": ["FI"],
                        "pbf": "https://example.invalid/finland.osm.pbf",
                    },
                    {
                        "id": "germany",
                        "name": "Germany",
                        "parent": "europe",
                        "iso2": ["DE"],
                        "pbf": "https://example.invalid/germany.osm.pbf",
                    },
                    {
                        "id": "bavaria",
                        "name": "Bavaria",
                        "parent": "germany",
                        "iso2": [],
                        "pbf": "https://example.invalid/bavaria.osm.pbf",
                    },
                    {
                        "id": "sweden",
                        "name": "Sweden",
                        "parent": "europe",
                        "iso2": ["SE"],
                        "pbf": "https://example.invalid/sweden.osm.pbf",
                    },
                ]
            )
            row = window.region_combos[0]
            assert isinstance(row, gui.RegionSelectionRow)
            assert row.arrow.text() == "→"
            assert row.remove_button.isHidden()

            labels = [
                row.parent_combo.itemText(i)
                for i in range(row.parent_combo.count())
            ]
            africa_at = labels.index("Africa")
            antarctica_at = labels.index("Antarctica")
            asia_at = labels.index("Asia")
            europe_at = labels.index("Europe")
            germany_at = labels.index("Germany — Europe")
            assert africa_at < antarctica_at < asia_at < europe_at < germany_at
            assert any("Europe" in label and "──" in label for label in labels)

            europe_index = row.parent_combo.findData(
                "europe", gui.Qt.ItemDataRole.UserRole
            )
            assert europe_index >= 0
            row.parent_combo.blockSignals(True)
            row.parent_combo.setCurrentIndex(europe_index)
            row.parent_combo.blockSignals(False)
            row._prefer_whole_after_parent_change = True
            window.region_size_cache["europe"] = 1_000_000
            window._region_selection_changed(row)
            assert row.region_id() == "europe"
            assert row.child_combo.findData(
                "finland", gui.Qt.ItemDataRole.UserRole
            ) >= 0
            assert "Download size:" in window.api_size_label.text()
            assert "Estimated Local OSM DB:" in window.api_size_label.text()

            window.region_size_cache.pop("europe", None)
            window.region_size_pending.add("europe")
            window._sync_build_button()
            assert window.build_button.isEnabled()
            window.region_size_pending.discard("europe")

            window._add_region_selector()
            assert len(window.region_combos) == 2
            second = window.region_combos[1]
            assert isinstance(second, gui.RegionSelectionRow)
            assert not second.remove_button.isHidden()
            assert second.remove_button.isEnabled()
            window._remove_region_selector(second)
            app.processEvents()
            assert len(window.region_combos) == 1
            assert window.region_combos[0] is row
            assert row.remove_button.isHidden()
        finally:
            window.close()
            app.processEvents()
    print("GUI offscreen smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
