from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local_osm.network_errors import NETWORK_ERROR_PREFIX

try:
    from local_osm.gui import app as gui
except ModuleNotFoundError as exc:
    if not (exc.name or "").startswith("PySide6"):
        raise
    gui = None


@unittest.skipIf(gui is None, "PySide6 is required for GUI output handling tests")
class GuiNetworkErrorTests(unittest.TestCase):
    def state(self, process, kind="build"):
        return SimpleNamespace(task_process=process, task_kind=kind,
                               _task_network_output="", _task_network_notified=False,
                               _show_api_unavailable=Mock())

    def test_network_marker_split_across_output_reads_notifies_once(self):
        process, target = Mock(), Mock()
        state = self.state(process)
        for chunk in (b"[error] Net", b"work request failed: timeout\n",
                      NETWORK_ERROR_PREFIX.encode() + b" another failure\n"):
            process.readAllStandardOutput.return_value = chunk
            gui.MainWindow._append_process_output(state, process, target)
        state._show_api_unavailable.assert_called_once()
        self.assertEqual(target.appendPlainText.call_count, 3)

    def test_large_output_chunk_does_not_discard_the_error_before_detection(self):
        process, target = Mock(), Mock()
        state = self.state(process)
        process.readAllStandardOutput.return_value = NETWORK_ERROR_PREFIX.encode() + b"x" * 8192
        gui.MainWindow._append_process_output(state, process, target)
        state._show_api_unavailable.assert_called_once()
        self.assertLessEqual(len(state._task_network_output), 4096)

    def test_other_processes_and_non_build_tasks_do_not_show_api_tooltip(self):
        for kind, active in (("server", True), ("build", False)):
            with self.subTest(kind=kind, active=active):
                process, target = Mock(), Mock()
                state = self.state(process if active else Mock(), kind)
                process.readAllStandardOutput.return_value = NETWORK_ERROR_PREFIX.encode()
                gui.MainWindow._append_process_output(state, process, target)
                state._show_api_unavailable.assert_not_called()

    def test_catalogue_failure_displays_recovery_advice_and_selects_local_files(self):
        state = SimpleNamespace(api_radio=Mock(), local_radio=Mock(), build_button=Mock())
        state._show_api_unavailable = lambda: gui.MainWindow._show_api_unavailable(state)
        with patch.object(gui.QToolTip, "showText") as show:
            gui.MainWindow._catalog_unavailable(state)
        state.api_radio.setEnabled.assert_called_once_with(False)
        state.local_radio.setChecked.assert_called_once_with(True)
        message = state.api_radio.setToolTip.call_args.args[0]
        self.assertIn("API is unavailable", message)
        self.assertIn("try again later", message)
        self.assertIn("Local .osm.pbf files", message)
        state.build_button.setToolTip.assert_called_once_with(message)
        self.assertEqual(show.call_args.args[1], message)

    def test_catalogue_recovery_clears_unavailable_tooltips(self):
        state = SimpleNamespace(api_radio=Mock(), build_button=Mock(), local_pbfs=[],
                               region_combos=[Mock()])
        gui.MainWindow._catalog_loaded(state, [])
        state.api_radio.setEnabled.assert_called_once_with(True)
        state.api_radio.setToolTip.assert_called_once_with("")
        state.build_button.setToolTip.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main()
