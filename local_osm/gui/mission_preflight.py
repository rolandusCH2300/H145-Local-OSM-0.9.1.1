from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

from ..mission_audit import MissionScan, scan_mission_queries


class MissionScanSignals(QObject):
    finished = Signal(object)


def scan_for_build(signals: MissionScanSignals, target: Path, command: list[str], root: str) -> None:
    try:
        scan, error = scan_mission_queries(root), None
    except Exception as exc:
        scan, error = None, str(exc)
    try:
        signals.finished.emit((target, command, scan, error))
    except RuntimeError:
        # The window may have closed while the background scan was running.
        pass


def confirm_mission_exclusions(parent, scan: MissionScan) -> list[str] | None:
    """Offer a build-scoped exclusion of the files whose reads failed."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Problematic mission files")
    dialog.resize(720, 400)
    layout = QVBoxLayout(dialog)
    label = QLabel(
        f"{len(scan.errors)} mission file(s) could not be read.\n\n"
        "Continue without these files for this build? Their mission queries will not "
        "be checked for coverage. The files on disk will remain unchanged."
    )
    label.setWordWrap(True)
    layout.addWidget(label)
    details = QPlainTextEdit()
    details.setReadOnly(True)
    paths = [str(Path(scan.packages_root) / item.path) for item in scan.errors]
    details.setPlainText("\n\n".join(
        f"{path}\n{item.error}" for path, item in zip(paths, scan.errors)
    ))
    layout.addWidget(details)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
    proceed = buttons.addButton("Continue without these files", QDialogButtonBox.ButtonRole.AcceptRole)
    proceed.setAutoDefault(False)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return paths if dialog.exec() == QDialog.DialogCode.Accepted else None
