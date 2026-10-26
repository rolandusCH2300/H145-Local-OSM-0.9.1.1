from __future__ import annotations

import sys
import threading
from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QProcess, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .. import geofabrik
from ..network_errors import NETWORK_ERROR_PREFIX
from .regions import conflicting_region_ids
from .mission_preflight import MissionScanSignals, confirm_mission_exclusions, scan_for_build
from .services import (
    LOCAL_OVERPASS_URL,
    PortableConfigStore,
    api_build_command,
    api_database_name,
    application_root,
    benchmark_command,
    choose_default_database,
    configure_hpg_connection,
    data_directory,
    detect_community_folder,
    discover_databases,
    discover_missions,
    fetch_geofabrik_catalog,
    hpg_connection_status,
    local_build_command,
    local_database_name,
    probe_local_server,
    remember_database,
    restore_hpg_connection,
    server_command,
    validation_command,
)
from .rne_patch import (
    patch_rne_mission,
    restore_rne_mission,
    rne_patch_status,
)


class CatalogWorker(QThread):
    loaded = Signal(object)
    unavailable = Signal()

    def run(self) -> None:
        try:
            self.loaded.emit(fetch_geofabrik_catalog())
        except Exception:
            self.unavailable.emit()


class RegionComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def populate(
        self,
        records: list[dict],
        current_id: str | None = None,
        excluded: set[str] | None = None,
    ) -> None:
        excluded = excluded or set()
        model = QStandardItemModel(self)
        placeholder = QStandardItem("Select a country or region…")
        placeholder.setData(None, Qt.ItemDataRole.UserRole)
        placeholder.setEnabled(False)
        model.appendRow(placeholder)

        records_by_id = {
            str(record.get("id") or ""): record
            for record in records
            if record.get("id")
        }
        grouped: dict[str, list[dict]] = defaultdict(list)
        for record in records:
            grouped[str(record.get("parent") or "Top level")].append(record)

        selected_index = 0
        row = 1
        for group in sorted(grouped, key=str.casefold):
            parent_record = records_by_id.get(group)
            if group != "Top level" and parent_record is not None:
                parent_id = str(parent_record.get("id") or "")
                parent_name = str(parent_record.get("name") or parent_id)
                header = QStandardItem(f"{parent_name} (whole region)")
                header.setData(parent_id, Qt.ItemDataRole.UserRole)
                if parent_id in excluded and parent_id != current_id:
                    header.setEnabled(False)
                if current_id and parent_id == current_id and selected_index == 0:
                    selected_index = row
            else:
                header = QStandardItem(group)
                header.setEnabled(False)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            model.appendRow(header)
            row += 1
            for record in sorted(
                grouped[group],
                key=lambda item: str(item.get("name") or "").casefold(),
            ):
                region_id = str(record.get("id") or "")
                label = str(record.get("name") or region_id)
                iso = ",".join(record.get("iso2") or [])
                if iso:
                    label = f"{label} ({iso})"
                item = QStandardItem(label)
                item.setData(region_id, Qt.ItemDataRole.UserRole)
                if region_id in excluded and region_id != current_id:
                    item.setEnabled(False)
                model.appendRow(item)
                if (
                    current_id
                    and region_id == current_id
                    and selected_index == 0
                ):
                    selected_index = row
                row += 1

        self.blockSignals(True)
        self.setModel(model)
        self.setCurrentIndex(selected_index)
        self.blockSignals(False)

    def region_id(self) -> str | None:
        value = self.currentData(Qt.ItemDataRole.UserRole)
        return str(value) if value else None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.root = application_root()
        self.store = PortableConfigStore(self.root)
        self.config = self.store.load()
        self.community: Path | None = detect_community_folder(
            self.config.get("community_folder")
        )
        if self.community is not None:
            self.store.update(community_folder=str(self.community))

        self.catalog: list[dict] = []
        self.catalog_by_id: dict[str, dict] = {}
        self.region_combos: list[RegionComboBox] = []
        self.local_pbfs: list[Path] = []
        self.task_process: QProcess | None = None
        self.task_kind: str | None = None
        self.task_success = None
        self.server_process: QProcess | None = None
        self.external_server = False
        self.pending_build_database: Path | None = None
        self._mission_scan_pending = False
        self._mission_scan_closing = False
        self._mission_scan_signals = MissionScanSignals(self)
        self._mission_scan_signals.finished.connect(self._mission_scan_finished)
        self.network_manager = QNetworkAccessManager(self)
        self.pending_size_replies: dict[QNetworkReply, dict] = {}
        self.pending_size_results: list[dict] = []
        self.pending_api_build: tuple[Path, list[str], tuple[str, ...]] | None = None

        self.setWindowTitle("H145 Local OSM")
        self.resize(1120, 760)

        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 16, 18, 16)
        root_layout.setSpacing(12)

        top = QHBoxLayout()
        title = QLabel("H145 Local OSM")
        font = title.font()
        font.setPointSize(16)
        font.setBold(True)
        title.setFont(font)
        top.addWidget(title)
        top.addStretch(1)
        self.setup_nav = QPushButton("Build")
        self.server_nav = QPushButton("Server")
        self.setup_nav.clicked.connect(
            lambda: self.pages.setCurrentWidget(self.setup_page)
        )
        self.server_nav.clicked.connect(
            lambda: self.pages.setCurrentWidget(self.server_page)
        )
        top.addWidget(self.setup_nav)
        top.addWidget(self.server_nav)
        root_layout.addLayout(top)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        root_layout.addWidget(line)

        self.pages = QStackedWidget()
        self.setup_page = self._build_setup_page()
        self.server_page = self._build_server_page()
        self.pages.addWidget(self.setup_page)
        self.pages.addWidget(self.server_page)
        root_layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        self._refresh_community()
        selected = self._refresh_databases()
        self.pages.setCurrentWidget(
            self.server_page if selected is not None else self.setup_page
        )

        self.catalog_worker = CatalogWorker(self)
        self.catalog_worker.loaded.connect(self._catalog_loaded)
        self.catalog_worker.unavailable.connect(self._catalog_unavailable)
        self.catalog_worker.start()

        self.server_probe_timer = QTimer(self)
        self.server_probe_timer.setInterval(2000)
        self.server_probe_timer.timeout.connect(self._refresh_server_state)
        self.server_probe_timer.start()
        self._refresh_server_state()

    def _build_setup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        columns = QHBoxLayout()
        columns.setSpacing(14)

        source_box = QGroupBox("OSM source")
        source_layout = QVBoxLayout(source_box)
        source_row = QHBoxLayout()
        self.api_radio = QRadioButton("Download from Geofabrik API")
        self.api_radio.setEnabled(False)
        self.api_radio.setToolTip("Loading Geofabrik API…")
        self.local_radio = QRadioButton("Local .osm.pbf files")
        self.local_radio.setChecked(True)
        self.api_radio.toggled.connect(self._source_mode_changed)
        self.local_radio.toggled.connect(self._source_mode_changed)
        source_row.addWidget(self.api_radio)
        source_row.addWidget(self.local_radio)
        source_row.addStretch(1)
        source_layout.addLayout(source_row)

        self.source_stack = QStackedWidget()
        api_widget = QWidget()
        self.api_rows_layout = QVBoxLayout(api_widget)
        self.api_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.api_rows_layout.addStretch(1)
        self.add_region_button = QPushButton("+ Add region")
        self.add_region_button.clicked.connect(self._add_region_selector)
        api_wrap = QWidget()
        api_wrap_layout = QVBoxLayout(api_wrap)
        api_wrap_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(api_widget)
        api_wrap_layout.addWidget(scroll, 1)
        api_wrap_layout.addWidget(self.add_region_button)
        self.source_stack.addWidget(api_wrap)

        local_widget = QWidget()
        local_layout = QVBoxLayout(local_widget)
        local_layout.setContentsMargins(0, 0, 0, 0)
        self.local_file_list = QListWidget()
        local_layout.addWidget(self.local_file_list, 1)
        local_buttons = QHBoxLayout()
        add_files = QPushButton("+ Add local PBF")
        add_files.clicked.connect(self._add_local_files)
        remove_files = QPushButton("Remove selected")
        remove_files.clicked.connect(self._remove_local_files)
        local_buttons.addWidget(add_files)
        local_buttons.addWidget(remove_files)
        local_buttons.addStretch(1)
        local_layout.addLayout(local_buttons)
        self.source_stack.addWidget(local_widget)
        self.source_stack.setCurrentIndex(1)
        source_layout.addWidget(self.source_stack, 1)
        columns.addWidget(source_box, 1)

        community_box = QGroupBox("MSFS Community and missions")
        community_layout = QVBoxLayout(community_box)
        self.community_button = QPushButton("No Community folder selected")
        self.community_button.setToolTip(
            "Click to select the MSFS Community folder."
        )
        self.community_button.clicked.connect(self._choose_community)
        community_layout.addWidget(self.community_button)
        self.mission_list = QListWidget()
        community_layout.addWidget(self.mission_list, 1)
        columns.addWidget(community_box, 1)

        layout.addLayout(columns, 1)

        self.setup_progress = QProgressBar()
        self.setup_progress.setRange(0, 0)
        self.setup_progress.hide()
        layout.addWidget(self.setup_progress)
        self.setup_log = QPlainTextEdit()
        self.setup_log.setReadOnly(True)
        self.setup_log.setMaximumBlockCount(4000)
        self.setup_log.setPlaceholderText("Build progress will appear here.")
        self.setup_log.setMaximumHeight(190)
        layout.addWidget(self.setup_log)

        bottom = QHBoxLayout()
        self.serial_checkbox = QCheckBox(
            "Limited-hardware topology (serial)"
        )
        self.serial_checkbox.setToolTip(
            "Uses the serial topology importer (--workers 0). "
            "It uses fewer hardware resources but is slower than the normal "
            "automatic worker mode."
        )
        bottom.addWidget(self.serial_checkbox)
        bottom.addStretch(1)
        self.build_button = QPushButton("Build database")
        self.build_button.clicked.connect(self._build_database)
        bottom.addWidget(self.build_button)
        layout.addLayout(bottom)
        return page

    def _build_server_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        database_box = QGroupBox("Database")
        database_layout = QHBoxLayout(database_box)
        database_layout.addWidget(QLabel("Database:"))
        self.database_combo = QComboBox()
        self.database_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.database_combo.currentIndexChanged.connect(self._database_changed)
        database_layout.addWidget(self.database_combo, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._refresh_databases())
        database_layout.addWidget(refresh)
        layout.addWidget(database_box)

        compatibility_box = QGroupBox("Compatibility patches")
        compatibility_layout = QVBoxLayout(compatibility_box)

        hpg_layout = QHBoxLayout()
        self.hpg_status_label = QLabel("HPG connection: checking…")
        hpg_layout.addWidget(self.hpg_status_label, 1)
        self.hpg_configure_button = QPushButton("Patch")
        self.hpg_configure_button.clicked.connect(self._configure_hpg)
        self.hpg_restore_button = QPushButton("Restore")
        self.hpg_restore_button.clicked.connect(self._restore_hpg)
        hpg_layout.addWidget(self.hpg_configure_button)
        hpg_layout.addWidget(self.hpg_restore_button)
        compatibility_layout.addLayout(hpg_layout)

        rne_layout = QHBoxLayout()
        self.rne_status_label = QLabel("R&E mission: checking…")
        rne_layout.addWidget(self.rne_status_label, 1)
        self.rne_patch_button = QPushButton("Patch")
        self.rne_patch_button.clicked.connect(self._patch_rne)
        self.rne_restore_button = QPushButton("Restore")
        self.rne_restore_button.clicked.connect(self._restore_rne)
        rne_layout.addWidget(self.rne_patch_button)
        rne_layout.addWidget(self.rne_restore_button)
        compatibility_layout.addLayout(rne_layout)

        layout.addWidget(compatibility_box)

        actions = QHBoxLayout()
        self.validate_button = QPushButton("Validate database")
        self.validate_button.clicked.connect(self._validate_database)
        self.benchmark_button = QPushButton("Benchmark database")
        self.benchmark_button.clicked.connect(self._benchmark_database)
        actions.addWidget(self.validate_button)
        actions.addWidget(self.benchmark_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.server_progress = QProgressBar()
        self.server_progress.setRange(0, 0)
        self.server_progress.hide()
        layout.addWidget(self.server_progress)

        self.server_log = QPlainTextEdit()
        self.server_log.setReadOnly(True)
        self.server_log.setMaximumBlockCount(5000)
        self.server_log.setPlaceholderText(
            "Validation, benchmark and server output will appear here."
        )
        layout.addWidget(self.server_log, 1)

        status_row = QHBoxLayout()
        self.server_status_label = QLabel("○ Server stopped")
        status_row.addWidget(self.server_status_label)
        status_row.addStretch(1)
        self.start_button = QPushButton("Start server")
        self.start_button.clicked.connect(self._start_server)
        self.stop_button = QPushButton("Stop server")
        self.stop_button.clicked.connect(self._stop_server)
        status_row.addWidget(self.start_button)
        status_row.addWidget(self.stop_button)
        layout.addLayout(status_row)
        return page

    def _source_mode_changed(self) -> None:
        self.source_stack.setCurrentIndex(
            0 if self.api_radio.isChecked() else 1
        )

    def _catalog_loaded(self, records: object) -> None:
        self.catalog = list(records)
        self.catalog_by_id = {
            str(record.get("id")): record for record in self.catalog
        }
        self.api_radio.setEnabled(True)
        self.api_radio.setToolTip("")
        self.build_button.setToolTip("")
        if not self.local_pbfs:
            self.api_radio.setChecked(True)
        if not self.region_combos:
            self._add_region_selector()

    def _catalog_unavailable(self) -> None:
        self.api_radio.setEnabled(False)
        self._show_api_unavailable()
        self.local_radio.setChecked(True)

    def _show_api_unavailable(self) -> None:
        message = (
            "The Geofabrik API is unavailable. Please try again later or download "
            "the files yourself and select 'Local .osm.pbf files'."
        )
        self.api_radio.setToolTip(message)
        self.build_button.setToolTip(message)
        QToolTip.showText(
            self.api_radio.mapToGlobal(self.api_radio.rect().bottomLeft()),
            message,
            self.api_radio,
        )

    def _add_region_selector(self) -> None:
        combo = RegionComboBox()
        combo.currentIndexChanged.connect(self._region_selection_changed)
        self.region_combos.append(combo)
        self.api_rows_layout.insertWidget(
            max(0, self.api_rows_layout.count() - 1), combo
        )
        self._refresh_region_choices()

    def _region_selection_changed(self) -> None:
        self._refresh_region_choices()

    def _refresh_region_choices(self) -> None:
        if not self.catalog:
            return
        current = [combo.region_id() for combo in self.region_combos]
        selected = {value for value in current if value}
        for combo, value in zip(self.region_combos, current):
            other_selected = selected - ({value} if value else set())
            combo.populate(
                self.catalog,
                value,
                conflicting_region_ids(self.catalog, other_selected),
            )

    def _add_local_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select local OSM PBF files",
            str(self.root),
            "OpenStreetMap PBF (*.osm.pbf);;All files (*)",
        )
        for raw in files:
            path = Path(raw).resolve()
            if path not in self.local_pbfs:
                self.local_pbfs.append(path)
        self._refresh_local_files()

    def _remove_local_files(self) -> None:
        selected = {
            item.text() for item in self.local_file_list.selectedItems()
        }
        self.local_pbfs = [
            path for path in self.local_pbfs if str(path) not in selected
        ]
        self._refresh_local_files()

    def _refresh_local_files(self) -> None:
        self.local_file_list.clear()
        for path in self.local_pbfs:
            self.local_file_list.addItem(str(path))

    def _choose_community(self) -> None:
        start = str(self.community or self.root)
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select MSFS Community folder",
            start,
        )
        if not selected:
            return
        self.community = Path(selected).resolve()
        self.store.update(community_folder=str(self.community))
        self._refresh_community()

    def _refresh_community(self) -> None:
        if self.community is None or not self.community.is_dir():
            self.community = None
            self.community_button.setText("No Community folder selected")
            self.community_button.setToolTip(
                "Click to select the MSFS Community folder."
            )
            self.mission_list.clear()
        else:
            self.community_button.setText(str(self.community))
            self.community_button.setToolTip(
                "Click to change the MSFS Community folder."
            )
            self.mission_list.clear()
            missions = discover_missions(self.community)
            current_package = None
            for mission in missions:
                if mission.package != current_package:
                    header = QListWidgetItem(mission.package)
                    header.setFlags(Qt.ItemFlag.NoItemFlags)
                    font = header.font()
                    font.setBold(True)
                    header.setFont(font)
                    self.mission_list.addItem(header)
                    current_package = mission.package
                self.mission_list.addItem(f"  {mission.relative_path}")
        self._refresh_hpg_status()
        self._refresh_rne_status()

    def _selected_database(self) -> Path | None:
        value = self.database_combo.currentData(
            Qt.ItemDataRole.UserRole
        )
        return Path(value) if value else None

    def _refresh_databases(
        self,
        preferred: Path | None = None,
    ) -> Path | None:
        databases = discover_databases(self.root)
        preferred_name = (
            preferred.name
            if preferred
            else self.store.load().get("last_database")
        )
        chosen = choose_default_database(databases, preferred_name)
        self.database_combo.blockSignals(True)
        self.database_combo.clear()
        for path in databases:
            self.database_combo.addItem(path.name, str(path))
        if chosen is not None:
            for index in range(self.database_combo.count()):
                if (
                    Path(self.database_combo.itemData(index)).name.lower()
                    == chosen.name.lower()
                ):
                    self.database_combo.setCurrentIndex(index)
                    break
            remember_database(self.store, chosen)
        self.database_combo.blockSignals(False)
        self._update_database_actions()
        return chosen

    def _database_changed(self) -> None:
        database = self._selected_database()
        if database is not None:
            remember_database(self.store, database)
        self._update_database_actions()

    def _update_database_actions(self) -> None:
        exists = self._selected_database() is not None
        busy = self.task_process is not None or self._mission_scan_pending
        self.validate_button.setEnabled(exists and not busy)
        self.benchmark_button.setEnabled(
            exists and not busy and not self._server_active()
        )
        self.start_button.setEnabled(
            exists and not self._server_active() and not busy
        )
        self.stop_button.setEnabled(
            self.server_process is not None
            and self.server_process.state()
            != QProcess.ProcessState.NotRunning
        )

    def _build_database(self) -> None:
        if self.task_process is not None or self.pending_size_replies or self._mission_scan_pending:
            return
        data_directory(self.root).mkdir(parents=True, exist_ok=True)
        serial = self.serial_checkbox.isChecked()

        api_records: list[dict] | None = None
        api_ids: tuple[str, ...] = ()
        if self.api_radio.isChecked():
            ids = [combo.region_id() for combo in self.region_combos]
            ids = [value for value in ids if value]
            if not ids:
                QMessageBox.information(
                    self,
                    "Select a region",
                    "Select at least one Geofabrik country or region.",
                )
                return
            api_ids = tuple(ids)
            api_records = [self.catalog_by_id[value] for value in ids]
            target = data_directory(self.root) / api_database_name(api_records)
            command = api_build_command(
                ids,
                target,
                self.community,
                serial_topology=serial,
            )
        else:
            if not self.local_pbfs:
                QMessageBox.information(
                    self,
                    "Select a local file",
                    "Select at least one local .osm.pbf file.",
                )
                return
            target = (
                data_directory(self.root)
                / local_database_name(self.local_pbfs)
            )
            command = local_build_command(
                self.local_pbfs,
                target,
                self.community,
                serial_topology=serial,
            )

        if target.exists():
            answer = QMessageBox.question(
                self,
                "Replace existing database?",
                f"{target.name} already exists. Replace it after a successful build?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        if api_records is not None:
            self._check_api_download_sizes(
                api_records,
                target,
                command,
                api_ids,
            )
            return

        self._start_build(target, command)

    def _start_build(self, target: Path, command: list[str]) -> None:
        if self.task_process is not None or self._mission_scan_pending:
            return
        self.setup_log.clear()
        if "--packages-root" in command:
            root = command[command.index("--packages-root") + 1]
            self._mission_scan_pending = True
            self.setup_log.appendPlainText("Checking installed mission files...")
            self.setup_progress.show()
            self.build_button.setEnabled(False)
            self._update_database_actions()
            threading.Thread(
                target=scan_for_build,
                args=(self._mission_scan_signals, target, list(command), root),
                daemon=True,
            ).start()
            return
        self._launch_build(target, command)

    def _mission_scan_finished(self, payload) -> None:
        if self._mission_scan_closing:
            return
        target, command, scan, error = payload
        try:
            if error is not None or (scan and any(Path(item.path).is_absolute() for item in scan.errors)):
                detail = error if error is not None else "\n".join(f"{item.path}: {item.error}" for item in scan.errors)
                QMessageBox.warning(self, "Mission check failed", detail)
                self.setup_log.appendPlainText(f"Mission check failed: {detail}")
                return
            if scan.errors:
                for item in scan.errors:
                    self.setup_log.appendPlainText(
                        f"[mission-audit] JSON read error: {Path(scan.packages_root) / item.path}: {item.error}"
                    )
                excluded = confirm_mission_exclusions(self, scan)
                if excluded is None:
                    self.setup_log.appendPlainText("Build canceled before download/import.")
                    return
                for path in excluded:
                    command.extend(["--exclude-mission-file", path])
            self._launch_build(target, command)
        finally:
            self._mission_scan_pending = False
            if self.task_process is None:
                self.setup_progress.hide()
                self.build_button.setEnabled(True)
            self._update_database_actions()

    def _launch_build(self, target: Path, command: list[str]) -> None:
        self.pending_build_database = target
        self._start_task(
            command,
            "build",
            self.setup_log,
            self._build_finished,
        )

    def _check_api_download_sizes(
        self,
        records: list[dict],
        target: Path,
        command: list[str],
        selected_ids: tuple[str, ...],
    ) -> None:
        self.pending_api_build = (target, command, selected_ids)
        self.pending_size_results = []
        self.setup_log.clear()
        self.setup_log.appendPlainText(
            "Checking selected Geofabrik download size..."
        )
        self.setup_progress.show()
        self.build_button.setEnabled(False)

        for record in records:
            url = str(record.get("pbf") or "")
            if not url:
                self.pending_size_results.append(
                    {
                        "id": str(record.get("id") or ""),
                        "name": str(record.get("name") or record.get("id") or "Unknown"),
                        "size": None,
                    }
                )
                continue

            request = QNetworkRequest(QUrl(url))
            request.setTransferTimeout(15000)
            reply = self.network_manager.head(request)
            self.pending_size_replies[reply] = record
            reply.finished.connect(
                lambda reply=reply: self._download_size_reply_finished(reply)
            )

        if not self.pending_size_replies:
            self._finish_api_download_size_check()

    def _download_size_reply_finished(self, reply: QNetworkReply) -> None:
        record = self.pending_size_replies.pop(reply, None)
        if record is None:
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

        self.pending_size_results.append(
            {
                "id": str(record.get("id") or ""),
                "name": str(record.get("name") or record.get("id") or "Unknown"),
                "size": size,
            }
        )
        reply.deleteLater()

        if not self.pending_size_replies:
            self._finish_api_download_size_check()

    def _finish_api_download_size_check(self) -> None:
        pending = self.pending_api_build
        self.pending_api_build = None
        self.setup_progress.hide()
        self.build_button.setEnabled(True)
        if pending is None:
            return

        target, command, selected_ids = pending
        current_ids = tuple(
            value
            for value in (combo.region_id() for combo in self.region_combos)
            if value
        )
        if current_ids != selected_ids:
            self.setup_log.appendPlainText(
                "Region selection changed; download size check canceled."
            )
            return

        rows = sorted(
            self.pending_size_results,
            key=lambda row: str(row.get("name") or "").casefold(),
        )
        known = [int(row["size"]) for row in rows if row.get("size") is not None]
        unknown = [row for row in rows if row.get("size") is None]
        known_total = sum(known)

        details = []
        for row in rows[:10]:
            size = row.get("size")
            size_text = geofabrik.human_bytes(int(size)) if size is not None else "unknown"
            details.append(f"{row['name']}: {size_text}")
        if len(rows) > 10:
            details.append(f"...and {len(rows) - 10} more")

        if unknown:
            if known:
                total_text = (
                    f"Known total: {geofabrik.human_bytes(known_total)} "
                    f"(+ {len(unknown)} unknown)"
                )
            else:
                total_text = "Total: unavailable"
        else:
            total_text = f"Total: {geofabrik.human_bytes(known_total)}"

        message = (
            "Selected Geofabrik source download:\n\n"
            + "\n".join(details)
            + f"\n\n{total_text}\n\n"
            + "This is the .osm.pbf download size only. The resulting Local OSM "
            + "database can be substantially larger and also needs working disk space.\n\n"
            + "Start the download and build?"
        )
        answer = QMessageBox.question(
            self,
            "Confirm Geofabrik download",
            message,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_build(target, command)
        else:
            self.setup_log.appendPlainText("Build canceled before download.")

    def _build_finished(self, exit_code: int) -> None:
        if (
            exit_code == 0
            and self.pending_build_database is not None
            and self.pending_build_database.is_file()
        ):
            chosen = self._refresh_databases(
                self.pending_build_database
            )
            if chosen is not None:
                remember_database(self.store, chosen)
                self.pages.setCurrentWidget(self.server_page)
        self.pending_build_database = None

    def _validate_database(self) -> None:
        database = self._selected_database()
        if database is None:
            return
        command = validation_command(
            database,
            self.community,
            self.root,
        )
        self._start_task(
            command,
            "validate",
            self.server_log,
            None,
        )

    def _benchmark_database(self) -> None:
        database = self._selected_database()
        if database is None or self._server_active():
            return
        command = benchmark_command(
            database,
            self.community,
            self.root,
        )
        self._start_task(
            command,
            "benchmark",
            self.server_log,
            None,
        )

    def _start_task(
        self,
        command: list[str],
        kind: str,
        output: QPlainTextEdit,
        success_callback,
    ) -> None:
        if self.task_process is not None:
            return
        process = QProcess(self)
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.setWorkingDirectory(str(self.root))
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.readyReadStandardOutput.connect(
            lambda: self._append_process_output(process, output)
        )
        process.finished.connect(
            lambda code, _status: self._task_finished(int(code))
        )
        self.task_process = process
        self._task_network_output = ""
        self._task_network_notified = False
        if kind == "build":
            self.build_button.setToolTip("")
        self.task_kind = kind
        self.task_success = success_callback
        if kind == "build":
            self.setup_progress.show()
            self.build_button.setEnabled(False)
        else:
            self.server_progress.show()
        self._update_database_actions()
        output.appendPlainText("$ " + " ".join(command))
        process.start()

    def _task_finished(self, exit_code: int) -> None:
        process = self.task_process
        if process is not None:
            self._append_process_output(
                process,
                self.setup_log
                if self.task_kind == "build"
                else self.server_log,
            )
        callback = self.task_success
        kind = self.task_kind
        self.task_process = None
        self.task_kind = None
        self.task_success = None
        self.setup_progress.hide()
        self.server_progress.hide()
        self.build_button.setEnabled(True)
        if kind != "build":
            self.server_log.appendPlainText(
                f"\nTask finished with exit code {exit_code}."
            )
        if callback is not None:
            callback(exit_code)
        self._update_database_actions()

    def _append_process_output(
        self,
        process: QProcess,
        target: QPlainTextEdit,
    ) -> None:
        data = bytes(process.readAllStandardOutput())
        if data:
            text = data.decode("utf-8", errors="replace")
            target.appendPlainText(
                text.rstrip()
            )
            if process is self.task_process and self.task_kind == "build":
                network_output = self._task_network_output + text
                if (
                    not self._task_network_notified
                    and NETWORK_ERROR_PREFIX in network_output
                ):
                    self._task_network_notified = True
                    self._show_api_unavailable()
                # Keep enough context to recognize a marker split between reads.
                self._task_network_output = network_output[-4096:]

    def _refresh_hpg_status(self) -> None:
        status = hpg_connection_status(self.community)
        if status.mission_system is None:
            self.hpg_status_label.setText(
                "HPG connection: Unavailable"
            )
            self.hpg_status_label.setToolTip(status.detail)
            self.hpg_configure_button.setEnabled(False)
            self.hpg_restore_button.setEnabled(False)
            return
        if status.configured:
            self.hpg_status_label.setText(
                "HPG connection: Patched for H145 Local OSM"
            )
        else:
            self.hpg_status_label.setText(
                "HPG connection: Not patched"
            )
        self.hpg_status_label.setToolTip(
            f"{status.mission_system}\nCurrent endpoint: "
            f"{status.current_url or status.detail}"
        )
        self.hpg_configure_button.setEnabled(
            not status.configured
        )
        self.hpg_restore_button.setEnabled(
            status.backup_available
        )

    def _configure_hpg(self) -> None:
        if self.community is None:
            return
        try:
            configure_hpg_connection(self.community)
        except ValueError as exc:
            if "non-standard URL" not in str(exc):
                QMessageBox.warning(
                    self,
                    "HPG connection",
                    str(exc),
                )
                return
            answer = QMessageBox.question(
                self,
                "Replace custom Overpass DE endpoint?",
                f"{exc}\n\nReplace it with:\n{LOCAL_OVERPASS_URL}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                configure_hpg_connection(
                    self.community,
                    allow_nonstandard=True,
                )
            except Exception as nested:
                QMessageBox.warning(
                    self,
                    "HPG connection",
                    str(nested),
                )
                return
        except Exception as exc:
            QMessageBox.warning(
                self,
                "HPG connection",
                str(exc),
            )
            return
        self._refresh_hpg_status()

    def _restore_hpg(self) -> None:
        if self.community is None:
            return
        answer = QMessageBox.question(
            self,
            "Restore HPG endpoint?",
            "Restore the original MissionSystem.json endpoint from the saved backup?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restore_hpg_connection(self.community)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "HPG connection",
                str(exc),
            )
        self._refresh_hpg_status()

    def _refresh_rne_status(self) -> None:
        status = rne_patch_status(self.community)
        if status.mission_file is None:
            self.rne_status_label.setText("R&E mission: Unavailable")
            self.rne_status_label.setToolTip(status.detail)
            self.rne_patch_button.setEnabled(False)
            self.rne_restore_button.setEnabled(False)
            return

        if status.patched:
            self.rne_status_label.setText(
                "R&E mission: Patched for H145 Local OSM"
            )
        elif status.supported:
            self.rne_status_label.setText("R&E mission: Not patched")
        else:
            version = status.version or "unknown"
            self.rne_status_label.setText(
                f"R&E mission: Unsupported version {version}"
            )

        detail = status.detail or f"R&E version: {status.version or 'unknown'}"
        self.rne_status_label.setToolTip(
            f"{status.mission_file}\n{detail}"
        )
        self.rne_patch_button.setEnabled(
            status.supported and not status.patched
        )
        self.rne_restore_button.setEnabled(status.backup_available)

    def _patch_rne(self) -> None:
        if self.community is None:
            return
        try:
            patch_rne_mission(self.community)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "R&E compatibility patch",
                str(exc),
            )
        self._refresh_rne_status()

    def _restore_rne(self) -> None:
        if self.community is None:
            return
        answer = QMessageBox.question(
            self,
            "Restore R&E mission file?",
            "Restore everywhere_all.json from the saved H145 Local OSM backup?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restore_rne_mission(self.community)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "R&E compatibility patch",
                str(exc),
            )
        self._refresh_rne_status()

    def _server_active(self) -> bool:
        own = (
            self.server_process is not None
            and self.server_process.state()
            != QProcess.ProcessState.NotRunning
        )
        return own or self.external_server

    def _refresh_server_state(self) -> None:
        own = (
            self.server_process is not None
            and self.server_process.state()
            != QProcess.ProcessState.NotRunning
        )
        if own:
            self.external_server = False
            self.server_status_label.setText("● Server running")
        else:
            self.external_server = probe_local_server()
            self.server_status_label.setText(
                "● Server running (external)"
                if self.external_server
                else "○ Server stopped"
            )
        self._update_database_actions()

    def _start_server(self) -> None:
        database = self._selected_database()
        if database is None or self._server_active():
            return
        command = server_command(database)
        process = QProcess(self)
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.setWorkingDirectory(str(self.root))
        process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        process.readyReadStandardOutput.connect(
            lambda: self._append_process_output(
                process,
                self.server_log,
            )
        )
        process.started.connect(self._refresh_server_state)
        process.finished.connect(
            lambda _code, _status: self._server_finished()
        )
        self.server_process = process
        self.server_log.appendPlainText("$ " + " ".join(command))
        process.start()
        self._update_database_actions()

    def _server_finished(self) -> None:
        if self.server_process is not None:
            self._append_process_output(
                self.server_process,
                self.server_log,
            )
        self.server_process = None
        self._refresh_server_state()

    def _stop_server(self) -> None:
        process = self.server_process
        if (
            process is None
            or process.state() == QProcess.ProcessState.NotRunning
        ):
            return
        process.terminate()
        QTimer.singleShot(
            2000,
            lambda: (
                process.kill()
                if process.state()
                != QProcess.ProcessState.NotRunning
                else None
            ),
        )

    def closeEvent(self, event) -> None:
        self._mission_scan_closing = True
        self.server_probe_timer.stop()
        for reply in list(self.pending_size_replies):
            try:
                reply.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            reply.abort()
            reply.deleteLater()
        self.pending_size_replies.clear()
        self.pending_api_build = None
        if (
            self.task_process is not None
            and self.task_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            self.task_process.kill()
        if (
            self.server_process is not None
            and self.server_process.state()
            != QProcess.ProcessState.NotRunning
        ):
            self.server_process.terminate()
            self.server_process.waitForFinished(1500)
            if (
                self.server_process.state()
                != QProcess.ProcessState.NotRunning
            ):
                self.server_process.kill()
        super().closeEvent(event)


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("H145 Local OSM")
    app.setOrganizationName("H145 Local OSM")
    window = MainWindow()
    window.show()
    return app.exec()
