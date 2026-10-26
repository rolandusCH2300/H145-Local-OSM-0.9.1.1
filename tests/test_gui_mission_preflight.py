from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import Mock, patch

from local_osm.mission_audit import MissionScan, ScanError

try:
    from local_osm.gui import app as gui
    from local_osm.gui import mission_preflight as popup
    from PySide6.QtCore import QTimer, QEventLoop
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPlainTextEdit
except ModuleNotFoundError as exc:
    if not (exc.name or "").startswith("PySide6"):
        raise
    gui = None


@unittest.skipIf(gui is None, "PySide6 is required for popup tests")
class MissionPreflightPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_background_scan_prompts_before_launch_in_current_gui(self):
        from local_osm.gui.enhanced_app_v2 import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broken = root / "Community" / "pack" / "hpgmission" / "bad.json"
            broken.parent.mkdir(parents=True)
            broken.write_text('{', encoding="utf-8")
            with (
                patch.object(gui, "application_root", return_value=root),
                patch.object(gui, "detect_community_folder", return_value=None),
                patch.object(gui.CatalogWorker, "start"),
                patch.object(gui, "probe_local_server", return_value=False),
                patch.object(gui, "confirm_mission_exclusions", return_value=[str(broken)]) as confirm,
            ):
                window = MainWindow()
                try:
                    with patch.object(window, "_launch_build") as launch:
                        loop = QEventLoop()
                        window._mission_scan_signals.finished.connect(loop.quit)
                        timer = QTimer()
                        timer.setSingleShot(True)
                        timer.timeout.connect(loop.quit)
                        timer.start(5000)
                        command = ["build-api", "--packages-root", str(root / "Community")]
                        window._start_build(root / "db.sqlite3", command)
                        launch.assert_not_called()
                        self.assertTrue(window._mission_scan_pending)
                        self.assertFalse(window.build_button.isEnabled())
                        loop.exec()
                        timer.stop()
                        confirm.assert_called_once()
                        launch.assert_called_once_with(
                            root / "db.sqlite3", command + ["--exclude-mission-file", str(broken)],
                        )
                        self.assertFalse(window._mission_scan_pending)
                finally:
                    window.close()
                    self.app.processEvents()

    def scan(self):
        return MissionScan(
            packages_root=str(Path("Community").resolve()),
            errors=[ScanError("pack/hpgmission/broken.json", "JSONDecodeError: line 2 column 1")],
        )

    def state(self):
        return SimpleNamespace(
            _mission_scan_closing=False, _mission_scan_pending=True,
            setup_log=Mock(), setup_progress=Mock(), build_button=Mock(),
            task_process=None, _launch_build=Mock(), _update_database_actions=Mock(),
        )

    def test_continue_passes_only_displayed_files_to_build(self):
        state, scan = self.state(), self.scan()
        path = str(Path(scan.packages_root) / scan.errors[0].path)
        with patch.object(gui, "confirm_mission_exclusions", return_value=[path]):
            gui.MainWindow._mission_scan_finished(state, (Path("db"), ["build-api"], scan, None))
        state._launch_build.assert_called_once_with(Path("db"), ["build-api", "--exclude-mission-file", path])
        self.assertFalse(state._mission_scan_pending)

    def test_cancel_does_not_launch_build(self):
        state = self.state()
        with patch.object(gui, "confirm_mission_exclusions", return_value=None):
            gui.MainWindow._mission_scan_finished(state, (Path("db"), [], self.scan(), None))
        state._launch_build.assert_not_called()
        self.assertFalse(state._mission_scan_pending)

    def test_missing_root_does_not_offer_to_ignore_it(self):
        state, scan = self.state(), self.scan()
        scan.errors = [ScanError(scan.packages_root, "packages root does not exist")]
        with patch.object(gui, "confirm_mission_exclusions") as confirm, patch.object(gui.QMessageBox, "warning"):
            gui.MainWindow._mission_scan_finished(state, (Path("db"), [], scan, None))
        confirm.assert_not_called()
        state._launch_build.assert_not_called()

    def test_clean_scan_starts_without_popup(self):
        state, scan = self.state(), self.scan()
        scan.errors = []
        with patch.object(gui, "confirm_mission_exclusions") as confirm:
            gui.MainWindow._mission_scan_finished(state, (Path("db"), ["build-local"], scan, None))
        confirm.assert_not_called()
        state._launch_build.assert_called_once_with(Path("db"), ["build-local"])

    def test_real_dialog_shows_file_and_reason_and_supports_both_buttons(self):
        app = QApplication.instance() or QApplication([])
        scan = self.scan()
        expected = str(Path(scan.packages_root) / scan.errors[0].path)
        for proceed in (False, True):
            captured = []

            def click_button():
                dialog = app.activeModalWidget()
                captured.append(dialog.findChild(QPlainTextEdit).toPlainText())
                buttons = dialog.findChild(QDialogButtonBox)
                if proceed:
                    button = next(b for b in buttons.buttons() if buttons.buttonRole(b) == QDialogButtonBox.ButtonRole.AcceptRole)
                else:
                    button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
                button.click()

            QTimer.singleShot(0, click_button)
            result = popup.confirm_mission_exclusions(None, scan)
            self.assertEqual(result, [expected] if proceed else None)
            self.assertIn(expected, captured[0])
            self.assertIn(scan.errors[0].error, captured[0])


if __name__ == "__main__":
    unittest.main()
