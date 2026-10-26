from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QWidget,
)

from .. import geofabrik
from .app import MainWindow as BaseMainWindow
from .regions import (
    DB_ESTIMATE_HIGH_MULTIPLIER,
    DB_ESTIMATE_LOW_MULTIPLIER,
    DB_ESTIMATE_MULTIPLIER,
    conflicting_region_ids,
    direct_children,
    estimate_database_size,
    region_selections_conflict,
    selectable_parent_records,
)
from .services import api_build_command, api_database_name, data_directory


_WHOLE_PARENT = "__h145_local_osm_whole_parent__"


class RegionSelectionRow(QWidget):
    """One visually connected Geofabrik parent -> child selection row."""

    selectionChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: list[dict] = []
        self._prefer_whole_after_parent_change = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.parent_combo = QComboBox()
        self.parent_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.parent_combo.setToolTip(
            "Select a Geofabrik parent region. If no child is selected, "
            "the parent extract itself is downloaded."
        )

        self.arrow = QLabel("→")
        self.arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.arrow.font()
        font.setBold(True)
        self.arrow.setFont(font)
        self.arrow.setToolTip("Parent region → optional child extract")

        self.child_combo = QComboBox()
        self.child_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.child_combo.setToolTip(
            "Optionally select a direct child extract. Leave this on the "
            "whole-parent option to download the parent."
        )

        layout.addWidget(self.parent_combo, 1)
        layout.addWidget(self.arrow)
        layout.addWidget(self.child_combo, 1)

        self.parent_combo.currentIndexChanged.connect(self._parent_changed)
        self.child_combo.currentIndexChanged.connect(self._child_changed)
        self.refresh([], set())

    def parent_id(self) -> str | None:
        value = self.parent_combo.currentData(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def child_id(self) -> str | None:
        value = self.child_combo.currentData(Qt.ItemDataRole.UserRole)
        if not value or value == _WHOLE_PARENT:
            return None
        return str(value)

    def region_id(self) -> str | None:
        """Return the extract this row currently resolves to."""
        parent_id = self.parent_id()
        if not parent_id:
            return None
        value = self.child_combo.currentData(Qt.ItemDataRole.UserRole)
        if value == _WHOLE_PARENT:
            return parent_id
        if value:
            return str(value)
        return None

    def _parent_changed(self) -> None:
        self._prefer_whole_after_parent_change = True
        self.selectionChanged.emit(self)

    def _child_changed(self) -> None:
        self._prefer_whole_after_parent_change = False
        self.selectionChanged.emit(self)

    @staticmethod
    def _set_item_enabled(combo: QComboBox, index: int, enabled: bool) -> None:
        model = combo.model()
        item = model.item(index) if hasattr(model, "item") else None
        if item is not None:
            item.setEnabled(enabled)

    def refresh(self, records: list[dict], excluded_effective: set[str]) -> None:
        """Rebuild dropdowns while preserving the current valid selection."""
        previous_parent = self.parent_id()
        previous_child = self.child_id()
        previous_child_data = self.child_combo.currentData(Qt.ItemDataRole.UserRole)
        previous_whole = previous_child_data == _WHOLE_PARENT
        prefer_whole = self._prefer_whole_after_parent_change
        self.records = list(records)

        records_by_id = {
            str(record.get("id") or ""): record
            for record in self.records
            if record.get("id")
        }

        self.parent_combo.blockSignals(True)
        self.parent_combo.clear()
        self.parent_combo.addItem("Select parent region…", None)
        self._set_item_enabled(self.parent_combo, 0, False)
        selected_parent_index = -1
        for record in selectable_parent_records(self.records):
            region_id = str(record.get("id") or "")
            name = str(record.get("name") or region_id)
            parent_id = str(record.get("parent") or "")
            if parent_id:
                parent_record = records_by_id.get(parent_id)
                parent_name = str((parent_record or {}).get("name") or parent_id)
                label = f"{name} — {parent_name}"
            else:
                label = name
            self.parent_combo.addItem(label, region_id)
            if previous_parent == region_id:
                selected_parent_index = self.parent_combo.count() - 1
        self.parent_combo.setCurrentIndex(selected_parent_index)
        self.parent_combo.blockSignals(False)

        active_parent = self.parent_id()
        self.child_combo.blockSignals(True)
        self.child_combo.clear()
        if not active_parent:
            self.child_combo.addItem("Select parent first…", None)
            self._set_item_enabled(self.child_combo, 0, False)
            self.child_combo.setCurrentIndex(-1)
            self.child_combo.blockSignals(False)
            self._prefer_whole_after_parent_change = False
            return

        parent_record = records_by_id.get(active_parent, {})
        parent_name = str(parent_record.get("name") or active_parent)
        self.child_combo.addItem(f"Whole {parent_name}", _WHOLE_PARENT)
        whole_allowed = active_parent not in excluded_effective
        self._set_item_enabled(self.child_combo, 0, whole_allowed)

        selected_child_index = -1
        for record in direct_children(self.records, active_parent):
            region_id = str(record.get("id") or "")
            label = str(record.get("name") or region_id)
            iso = ",".join(record.get("iso2") or [])
            if iso:
                label = f"{label} ({iso})"
            self.child_combo.addItem(label, region_id)
            index = self.child_combo.count() - 1
            self._set_item_enabled(
                self.child_combo,
                index,
                region_id not in excluded_effective,
            )
            if previous_child == region_id and region_id not in excluded_effective:
                selected_child_index = index

        if selected_child_index >= 0:
            self.child_combo.setCurrentIndex(selected_child_index)
        elif (previous_whole or prefer_whole) and whole_allowed:
            self.child_combo.setCurrentIndex(0)
        elif previous_parent == active_parent and whole_allowed and previous_child_data is None:
            self.child_combo.setCurrentIndex(0)
        else:
            self.child_combo.setCurrentIndex(-1)

        self.child_combo.blockSignals(False)
        self._prefer_whole_after_parent_change = False


class MainWindow(BaseMainWindow):
    """GUI with parent/child region rows and live source/DB size estimates."""

    def __init__(self):
        self.region_size_cache: dict[str, int | None] = {}
        self.region_size_replies: dict[QNetworkReply, str] = {}
        super().__init__()

    def _build_setup_page(self) -> QWidget:
        page = super()._build_setup_page()

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        parent_label = QLabel("Parent region")
        child_label = QLabel("Child / subregion (optional)")
        arrow_label = QLabel("→")
        arrow_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(parent_label, 1)
        header_layout.addWidget(arrow_label)
        header_layout.addWidget(child_label, 1)
        self.api_rows_layout.insertWidget(0, header)

        self.api_size_label = QLabel(
            "Download size: select a region\nEstimated Local OSM DB: —"
        )
        self.api_size_label.setWordWrap(True)
        self.api_size_label.setToolTip(
            "Database estimate is calibrated from three known builds. "
            f"Central estimate {DB_ESTIMATE_MULTIPLIER:.1f}× source PBF; "
            f"observed range {DB_ESTIMATE_LOW_MULTIPLIER:.1f}–"
            f"{DB_ESTIMATE_HIGH_MULTIPLIER:.1f}×."
        )
        api_wrap = self.source_stack.widget(0)
        api_wrap_layout = api_wrap.layout()
        api_wrap_layout.insertWidget(
            max(0, api_wrap_layout.count() - 1),
            self.api_size_label,
        )
        return page

    def _catalog_loaded(self, records: object) -> None:
        super()._catalog_loaded(records)
        self._refresh_region_choices()
        self._queue_selected_size_requests()
        self._refresh_size_summary()

    def _catalog_unavailable(self) -> None:
        super()._catalog_unavailable()
        self.api_size_label.setText(
            "Download size: Geofabrik unavailable\nEstimated Local OSM DB: —"
        )

    def _add_region_selector(self) -> None:
        row = RegionSelectionRow()
        row.selectionChanged.connect(self._region_selection_changed)
        self.region_combos.append(row)
        self.api_rows_layout.insertWidget(
            max(1, self.api_rows_layout.count() - 1),
            row,
        )
        self._refresh_region_choices()
        self._queue_selected_size_requests()
        self._refresh_size_summary()

    def _region_selection_changed(self, changed_row: RegionSelectionRow) -> None:
        other_ids = [
            row.region_id()
            for row in self.region_combos
            if row is not changed_row and row.region_id()
        ]
        changed_row.refresh(
            self.catalog,
            conflicting_region_ids(self.catalog, other_ids),
        )
        self._refresh_region_choices()
        self._queue_selected_size_requests()
        self._refresh_size_summary()

    def _refresh_region_choices(self) -> None:
        if not self.catalog:
            return
        for row in self.region_combos:
            other_ids = [
                other.region_id()
                for other in self.region_combos
                if other is not row and other.region_id()
            ]
            row.refresh(
                self.catalog,
                conflicting_region_ids(self.catalog, other_ids),
            )

    def _selected_api_ids(self) -> list[str]:
        return [
            region_id
            for region_id in (row.region_id() for row in self.region_combos)
            if region_id
        ]

    def _queue_selected_size_requests(self) -> None:
        if not self.catalog_by_id:
            return
        pending_ids = set(self.region_size_replies.values())
        for region_id in self._selected_api_ids():
            if region_id in self.region_size_cache or region_id in pending_ids:
                continue
            record = self.catalog_by_id.get(region_id)
            url = str((record or {}).get("pbf") or "")
            if not url:
                self.region_size_cache[region_id] = None
                continue
            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(15000)
            reply = self.network_manager.head(request)
            self.region_size_replies[reply] = region_id
            reply.finished.connect(
                lambda reply=reply: self._region_size_reply_finished(reply)
            )
        self._sync_build_button()

    def _region_size_reply_finished(self, reply: QNetworkReply) -> None:
        region_id = self.region_size_replies.pop(reply, None)
        if region_id is None:
            reply.deleteLater()
            return

        size: int | None = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            raw = bytes(reply.rawHeader(b"Content-Length"))
            if raw:
                try:
                    size = int(raw.decode("ascii"))
                except (ValueError, UnicodeDecodeError):
                    size = None
        self.region_size_cache[region_id] = size
        reply.deleteLater()
        self._refresh_size_summary()
        self._sync_build_button()

    def _size_state(self) -> tuple[list[str], int, list[str], list[str]]:
        selected = self._selected_api_ids()
        known_total = 0
        pending: list[str] = []
        unknown: list[str] = []
        pending_ids = set(self.region_size_replies.values())
        for region_id in selected:
            if region_id in self.region_size_cache:
                size = self.region_size_cache[region_id]
                if size is None:
                    unknown.append(region_id)
                else:
                    known_total += int(size)
            elif region_id in pending_ids:
                pending.append(region_id)
            else:
                pending.append(region_id)
        return selected, known_total, pending, unknown

    def _refresh_size_summary(self) -> None:
        if not hasattr(self, "api_size_label"):
            return
        selected, known_total, pending, unknown = self._size_state()
        if not selected:
            self.api_size_label.setText(
                "Download size: select a region\nEstimated Local OSM DB: —"
            )
            self._sync_build_button()
            return

        if pending:
            known_text = geofabrik.human_bytes(known_total) if known_total else "0 B"
            self.api_size_label.setText(
                f"Download size: {known_text} known + checking {len(pending)}…\n"
                "Estimated Local OSM DB: checking source sizes…"
            )
            self._sync_build_button()
            return

        source_text = geofabrik.human_bytes(known_total)
        if unknown:
            source_text += f" + {len(unknown)} unknown"

        if known_total:
            central, low, high = estimate_database_size(known_total)
            estimate_text = f"~{geofabrik.human_bytes(central)}"
            if unknown:
                estimate_text += f" + {len(unknown)} unknown source(s)"
            range_text = (
                f"observed range {geofabrik.human_bytes(low)}–"
                f"{geofabrik.human_bytes(high)}"
            )
        else:
            estimate_text = "unavailable"
            range_text = "source size unavailable"

        self.api_size_label.setText(
            f"Download size: {source_text}\n"
            f"Estimated Local OSM DB: {estimate_text} ({range_text})"
        )
        self._sync_build_button()

    def _sync_build_button(self) -> None:
        if not hasattr(self, "build_button"):
            return
        if self.task_process is not None or self._mission_scan_pending:
            self.build_button.setEnabled(False)
            return
        if not self.api_radio.isChecked():
            self.build_button.setEnabled(True)
            return
        selected, _known, pending, _unknown = self._size_state()
        self.build_button.setEnabled(bool(selected) and not pending)

    def _source_mode_changed(self) -> None:
        super()._source_mode_changed()
        if self.api_radio.isChecked():
            self._queue_selected_size_requests()
            self._refresh_size_summary()
        self._sync_build_button()

    def _build_database(self) -> None:
        if not self.api_radio.isChecked():
            super()._build_database()
            return
        if self.task_process is not None or self._mission_scan_pending:
            return

        ids = self._selected_api_ids()
        if not ids:
            QMessageBox.information(
                self,
                "Select a region",
                "Select at least one Geofabrik parent or child region.",
            )
            return
        if region_selections_conflict(self.catalog, ids):
            QMessageBox.warning(
                self,
                "Overlapping regions",
                "The selected Geofabrik extracts overlap. Choose either a parent "
                "or its descendant, not both.",
            )
            return

        selected, known_total, pending, unknown = self._size_state()
        if pending:
            self._queue_selected_size_requests()
            self._refresh_size_summary()
            return

        records = [self.catalog_by_id[value] for value in selected]
        target = data_directory(self.root) / api_database_name(records)
        command = api_build_command(
            selected,
            target,
            self.community,
            serial_topology=self.serial_checkbox.isChecked(),
        )

        if target.exists():
            answer = QMessageBox.question(
                self,
                "Replace existing database?",
                f"{target.name} already exists. Replace it after a successful build?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        source_text = geofabrik.human_bytes(known_total)
        if unknown:
            source_text += f" + {len(unknown)} unknown"
        if known_total:
            central, low, high = estimate_database_size(known_total)
            db_text = (
                f"~{geofabrik.human_bytes(central)} "
                f"(observed range {geofabrik.human_bytes(low)}–"
                f"{geofabrik.human_bytes(high)})"
            )
        else:
            db_text = "unavailable"

        answer = QMessageBox.question(
            self,
            "Confirm Geofabrik download",
            "Selected Geofabrik source download:\n"
            f"{source_text}\n\n"
            "Estimated Local OSM database:\n"
            f"{db_text}\n\n"
            f"The estimate uses an empirical {DB_ESTIMATE_MULTIPLIER:.1f}× "
            "source-PBF multiplier from known builds. Actual database size can "
            "vary with region content and mission requirements. Additional "
            "temporary working disk space is also required.\n\n"
            "Start the download and build?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_build(target, command)

    def closeEvent(self, event) -> None:
        for reply in list(self.region_size_replies):
            try:
                reply.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            reply.abort()
            reply.deleteLater()
        self.region_size_replies.clear()
        super().closeEvent(event)


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("H145 Local OSM")
    app.setOrganizationName("H145 Local OSM")
    window = MainWindow()
    window.show()
    return app.exec()
