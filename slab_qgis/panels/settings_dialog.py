"""Dialog to edit a model's runtime settings, built from its manifest ``runtime`` table."""

from __future__ import annotations

from typing import Any

from qgis.gui import QgsFileWidget
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from slab_qgis import settings


class RuntimeSettingsDialog(QDialog):
    """One editable field per runtime setting the manifest declares, persisted on OK."""

    def __init__(self, manifest: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manifest = manifest
        self.setWindowTitle(f"{manifest.name} — model settings")

        root = QVBoxLayout(self)
        form = QFormLayout()
        self._widgets: dict[str, QWidget] = {}
        for var, spec in manifest.runtime.items():
            widget = _make_widget(str(spec.kind))
            _set_value(widget, settings.runtime_value(manifest.name, var))
            widget.setToolTip(spec.description or "")
            label = QLabel(var if spec.required else f"{var} (optional)")
            label.setToolTip(spec.description or "")
            form.addRow(label, widget)
            self._widgets[var] = widget
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save_and_accept(self) -> None:
        for var, widget in self._widgets.items():
            settings.set_runtime_value(self._manifest.name, var, _value(widget))
        self.accept()


def _make_widget(kind: str) -> QWidget:
    if kind in ("file", "directory"):
        picker = QgsFileWidget()
        picker.setStorageMode(
            QgsFileWidget.StorageMode.GetDirectory
            if kind == "directory"
            else QgsFileWidget.StorageMode.GetFile
        )
        return picker
    return QLineEdit()


def _set_value(widget: QWidget, value: str) -> None:
    if isinstance(widget, QgsFileWidget):
        widget.setFilePath(value)
    elif isinstance(widget, QLineEdit):
        widget.setText(value)


def _value(widget: QWidget) -> str:
    if isinstance(widget, QgsFileWidget):
        return widget.filePath().strip()
    if isinstance(widget, QLineEdit):
        return widget.text().strip()
    return ""
