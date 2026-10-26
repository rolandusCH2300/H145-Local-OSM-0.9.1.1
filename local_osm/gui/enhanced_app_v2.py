from __future__ import annotations

import sys
import threading
from collections import defaultdict

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QWidget,
)

from .. import geofabrik
from .app import MainWindow as BaseAppMainWindow
from .enhanced_app import (
    MainWindow as PreviousEnhancedMainWindow,
    RegionSelectionRow as PreviousRegionSelectionRow,
)
from .regions import (
    direct_children,
    region_selections_conflict,
    selectable_parent_records,
)
from .services import api_build_command, api_database_name, data_directory


_WHOLE_PARENT = "__h145_local_osm_whole_parent__"
_ARROW_COLUMN_WIDTH = 18
_REMOVE_COLUMN_WIDTH = 28
_COMBO_MINIMUM_WIDTH = 190
_PARENT_LABEL_LEFT_PADDING = 4


class _SizeSignals(QObject):
    resolved = Signal(str, object)


def _record_name(record: dict) -> str:
    return str(record.get("name") or record.get("id") or "")


def grouped_parent_records(
    records: list[dict],
) -> tuple[list[dict], list[tuple[str, list[dict]]]]:
    """Return top-level parents first, then nested parents grouped by root."""
    selectable = selectable_parent_records(records)
    records_by_id = {
        str(record.get("id") or ""): record
        for record in records
        if record.get("id")
    }

    top_level = [record for record in selectable if not record.get("parent")]
    top_level.sort(key=lambda record: _record_name(record).casefold())

    nested: dict[str, list[dict]] = defaultdict(list)
    for record in selectable:
        if not record.get("parent"):
            continue

        current = record
        seen: set[str] = set()
        while current.get("parent"):
            current_id = str(current.get("id") or "")
            if current_id in seen:
                break
            seen.add(current_id)
            parent_id = str(current.get("parent") or "")
            parent_record = records_by_id.get(parent_id)
            if parent_record is None:
                break
            current = parent_record

        root_name = _record_name(current) or str(current.get("id") or "")
        nested[root_name].append(record)

    groups = []
    for root_name in sorted(nested, key=str.casefold):
        values = sorted(
            nested[root_name],
            key=lambda record: _record_name(record).casefold(),
        )
        groups.append((root_name, values))
    return top_level, groups


class RegionSelectionRow(PreviousRegionSelectionRow):
    """Parent/child row with stable columns, grouping and row removal."""

    removeRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_combo.setMinimumWidth(_COMBO_MINIMUM_WIDTH)
        self.child_combo.setMinimumWidth(_COMBO_MINIMUM_WIDTH)
        self.arrow.setFixedWidth(_ARROW_COLUMN_WIDTH)

        self.remove_button = QPushButton("×")
        self.remove_button.setFixedWidth(_REMOVE_COLUMN_WIDTH)
        self.remove_button.setToolTip("Remove this region row.")
        self.remove_button.clicked.connect(lambda: self.removeRequested.emit(self))

        self.remove_spacer = QWidget()
        self.remove_spacer.setFixedWidth(_REMOVE_COLUMN_WIDTH)

        self.layout().addWidget(self.remove_button)
        self.layout().addWidget(self.remove_spacer)
        self.set_removable(False)

    def set_removable(self, removable: bool) -> None:
        self.remove_button.setVisible(removable)
        self.remove_button.setEnabled(removable)
        self.remove_spacer.setVisible(not removable)

    def _add_parent_separator(self, label: str) -> None:
        self.parent_combo.addItem(f"──────── {label} ────────", None)
        self._set_item_enabled(
            self.parent_combo,
            self.parent_combo.count() - 1,
            False,
        )

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

        top_level, nested_groups = grouped_parent_records(self.records)
        for record in top_level:
            region_id = str(record.get("id") or "")
            name = _record_name(record) or region_id
            self.parent_combo.addItem(name, region_id)
            if previous_parent == region_id:
                selected_parent_index = self.parent_combo.count() - 1

        for root_name, grouped_records in nested_groups:
            self._add_parent_separator(root_name)
            for record in grouped_records:
                region_id = str(record.get("id") or "")
                name = _record_name(record) or region_id
                parent_id = str(record.get("parent") or "")
                parent_record = records_by_id.get(parent_id)
                parent_name = _record_name(parent_record or {}) or parent_id
                self.parent_combo.addItem(f"{name} — {parent_name}", region_id)
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
        parent_name = _record_name(parent_record) or active_parent
        self.child_combo.addItem(f"Whole {parent_name}", _WHOLE_PARENT)
        whole_allowed = active_parent not in excluded_effective
        self._set_item_enabled(self.child_combo, 0, whole_allowed)

        selected_child_index = -1
        for record in direct_children(self.records, active_parent):
            region_id = str(record.get("id") or "")
            label = _record_name(record) or region_id
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
        elif (
            previous_parent == active_parent
            and whole_allowed
            and previous_child_data is None
        ):
            self.child_combo.setCurrentIndex(0)
        else:
            self.child_combo.setCurrentIndex(-1)

        self.child_combo.blockSignals(False)
        self._prefer_whole_after_parent_change = False


class MainWindow(PreviousEnhancedMainWindow):
    """Final region selector layout and reliable live Geofabrik size probes."""

    def __init__(self):
        self.region_size_pending: set[str] = set()
        self._size_signals = _SizeSignals()
        self._size_signals.resolved.connect(self._region_size_resolved)
        self._size_probe_closing = False
        self._allow_pending_size_build = False
        super().__init__()

        self.setup_nav.setCheckable(True)
        self.server_nav.setCheckable(True)
        self.setup_nav.setAutoExclusive(True)
        self.server_nav.setAutoExclusive(True)
        self.pages.currentChanged.connect(self._sync_navigation_state)
        self._sync_navigation_state(self.pages.currentIndex())

    def _sync_navigation_state(self, _index: int) -> None:
        setup_active = self.pages.currentWidget() is self.setup_page
        server_active = self.pages.currentWidget() is self.server_page
        self.setup_nav.setChecked(setup_active)
        self.server_nav.setChecked(server_active)

        setup_font = self.setup_nav.font()
        setup_font.setBold(setup_active)
        self.setup_nav.setFont(setup_font)

        server_font = self.server_nav.font()
        server_font.setBold(server_active)
        self.server_nav.setFont(server_font)

    def _build_setup_page(self) -> QWidget:
        # Align header columns with the selector rows.
        page = BaseAppMainWindow._build_setup_page(self)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)
        parent_label = QLabel("Parent region")
        parent_label.setContentsMargins(_PARENT_LABEL_LEFT_PADDING, 0, 0, 0)
        parent_label.setMinimumWidth(_COMBO_MINIMUM_WIDTH)
        child_label = QLabel("Child / subregion (optional)")
        child_label.setMinimumWidth(_COMBO_MINIMUM_WIDTH)
        arrow_spacer = QWidget()
        arrow_spacer.setFixedWidth(_ARROW_COLUMN_WIDTH)
        remove_spacer = QWidget()
        remove_spacer.setFixedWidth(_REMOVE_COLUMN_WIDTH)
        header_layout.addWidget(parent_label, 1)
        header_layout.addWidget(arrow_spacer)
        header_layout.addWidget(child_label, 1)
        header_layout.addWidget(remove_spacer)
        self.api_rows_layout.insertWidget(0, header)

        self.api_size_label = QLabel(
            "Download size: select a region\nEstimated Local OSM DB: —"
        )
        self.api_size_label.setWordWrap(True)
        self.api_size_label.setToolTip(
            "The Local OSM size estimate is an approximation based on test builds and can be inaccurate"
        )
        api_wrap = self.source_stack.widget(0)
        api_wrap_layout = api_wrap.layout()
        api_wrap_layout.insertWidget(
            max(0, api_wrap_layout.count() - 1),
            self.api_size_label,
        )
        return page

    def _add_region_selector(self) -> None:
        row = RegionSelectionRow()
        row.selectionChanged.connect(self._region_selection_changed)
        row.removeRequested.connect(self._remove_region_selector)
        self.region_combos.append(row)
        self.api_rows_layout.insertWidget(
            max(1, self.api_rows_layout.count() - 1),
            row,
        )
        self._update_row_remove_buttons()
        self._refresh_region_choices()
        self._queue_selected_size_requests()
        self._refresh_size_summary()

    def _remove_region_selector(self, row: RegionSelectionRow) -> None:
        if len(self.region_combos) <= 1 or row not in self.region_combos:
            return
        self.region_combos.remove(row)
        self.api_rows_layout.removeWidget(row)
        row.deleteLater()
        self._update_row_remove_buttons()
        self._refresh_region_choices()
        self._queue_selected_size_requests()
        self._refresh_size_summary()
        self._sync_build_button()

    def _update_row_remove_buttons(self) -> None:
        for index, row in enumerate(self.region_combos):
            if isinstance(row, RegionSelectionRow):
                row.set_removable(index > 0)

    def _queue_selected_size_requests(self) -> None:
        if not self.catalog_by_id or self._size_probe_closing:
            return
        for region_id in self._selected_api_ids():
            if (
                region_id in self.region_size_cache
                or region_id in self.region_size_pending
            ):
                continue
            record = self.catalog_by_id.get(region_id)
            url = str((record or {}).get("pbf") or "")
            if not url:
                self.region_size_cache[region_id] = None
                continue

            self.region_size_pending.add(region_id)
            thread = threading.Thread(
                target=self._resolve_region_size,
                args=(region_id, url, self._size_signals),
                name=f"h145-size-{region_id}",
                daemon=True,
            )
            thread.start()
        self._sync_build_button()

    @staticmethod
    def _resolve_region_size(
        region_id: str,
        url: str,
        signals: _SizeSignals,
    ) -> None:
        # Use the downloader's HEAD/Range size probe.
        try:
            size = geofabrik.remote_size(url)
        except Exception:
            size = None
        try:
            signals.resolved.emit(region_id, size)
        except RuntimeError:
            pass

    def _region_size_resolved(self, region_id: str, size: object) -> None:
        if self._size_probe_closing:
            return
        self.region_size_pending.discard(region_id)
        if size is None:
            self.region_size_cache[region_id] = None
        else:
            try:
                self.region_size_cache[region_id] = max(0, int(size))
            except (TypeError, ValueError):
                self.region_size_cache[region_id] = None
        self._refresh_size_summary()
        self._sync_build_button()

    def _size_state(self) -> tuple[list[str], int, list[str], list[str]]:
        selected = self._selected_api_ids()
        known_total = 0
        pending: list[str] = []
        unknown: list[str] = []
        for region_id in selected:
            if region_id in self.region_size_cache:
                size = self.region_size_cache[region_id]
                if size is None:
                    unknown.append(region_id)
                else:
                    known_total += int(size)
            elif region_id in self.region_size_pending:
                pending.append(region_id)
            else:
                pending.append(region_id)

        if self._allow_pending_size_build and pending:
            unknown.extend(pending)
            pending = []
        return selected, known_total, pending, unknown

    def _sync_build_button(self) -> None:
        if not hasattr(self, "build_button"):
            return
        if self.task_process is not None or self._mission_scan_pending:
            self.build_button.setEnabled(False)
            return
        if not self.api_radio.isChecked():
            self.build_button.setEnabled(True)
            return
        self.build_button.setEnabled(bool(self._selected_api_ids()))

    def _build_database(self) -> None:
        # Size probing is advisory only. If Geofabrik does not expose a size,
        # or a background probe is still running, building must remain possible.
        self._allow_pending_size_build = True
        try:
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

            selected, _known_total, pending, _unknown = self._size_state()
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

            # Confirm only when replacing an existing database.
            if target.exists():
                answer = QMessageBox.question(
                    self,
                    "Replace existing database?",
                    f"{target.name} already exists. Replace it after a successful build?",
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

            self._start_build(target, command)
        finally:
            self._allow_pending_size_build = False

    def closeEvent(self, event) -> None:
        self._size_probe_closing = True
        try:
            self._size_signals.resolved.disconnect(self._region_size_resolved)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("H145 Local OSM")
    app.setOrganizationName("H145 Local OSM")
    window = MainWindow()
    window.show()
    return app.exec()
