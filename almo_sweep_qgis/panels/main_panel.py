"""The main dockable panel: a model selector plus a navigator over the workflow sections.

The sections mirror the study workflow end to end:

1. **Parameters** -- the model's inputs *and* parameters, built from its manifest (the DEM and
   release-zone layers are chosen here, beside the parameters that select them).
2. **Study**      -- deterministic run or a probabilistic sweep (distributions, samples).
3. **Run**        -- name, output folder, output selection, launch and monitor.
4. **Results**    -- browse and inspect the runs produced.

A model manifest is selected once at the top; every page reads the active model (and the
user's selections) from a shared :class:`~almo_sweep_qgis.state.StudyState`.
"""

from __future__ import annotations

from qgis.gui import QgsDockWidget, QgsFileWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.parameters_page import ParametersPage
from almo_sweep_qgis.panels.run_page import RunPage
from almo_sweep_qgis.panels.settings_dialog import RuntimeSettingsDialog
from almo_sweep_qgis.state import StudyState

#: Ordered section titles. The order is the intended top-to-bottom workflow.
PAGE_TITLES = ("Parameters", "Study", "Run", "Results")


class MainPanel(QgsDockWidget):
    """A dock widget: model selector on top, section navigator + page stack below."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("ALMO Sweep", parent)
        # A stable object name lets QGIS save and restore the dock's geometry/state.
        self.setObjectName("AlmoSweepPanel")
        self.state = StudyState(self)

        container = QWidget(self)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.addLayout(self._build_header())

        body = QHBoxLayout()
        self._nav = QListWidget(container)
        self._nav.setMaximumWidth(140)
        self._nav.addItems(PAGE_TITLES)

        self._stack = QStackedWidget(container)
        # Pages are added in PAGE_TITLES order; real pages replace placeholders as built.
        self._stack.addWidget(ParametersPage(self.state))  # 0: Parameters (inputs + params)
        self._stack.addWidget(_placeholder_page("Study"))  # 1: Study
        self._stack.addWidget(RunPage(self.state))  # 2: Run
        self._stack.addWidget(_placeholder_page("Results"))  # 3: Results

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)
        body.addWidget(self._nav, 0)
        body.addWidget(self._stack, 1)
        outer.addLayout(body, 1)

        self.setWidget(container)

    # -- model selector ----------------------------------------------------------------
    def _build_header(self) -> QVBoxLayout:
        header = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(QLabel("Model:"))
        self._model_file = QgsFileWidget()
        self._model_file.setStorageMode(QgsFileWidget.StorageMode.GetFile)
        self._model_file.setDialogTitle("Select a model manifest (model.toml)")
        self._model_file.setFilter("Model manifest (*.toml);;All files (*.*)")
        self._model_file.fileChanged.connect(self._on_model_file_changed)
        row.addWidget(self._model_file, 1)

        self._settings_button = QPushButton("Model settings…")
        self._settings_button.setToolTip("Configure this model's runtime settings")
        self._settings_button.setEnabled(False)
        self._settings_button.clicked.connect(self._open_settings)
        row.addWidget(self._settings_button, 0)
        header.addLayout(row)

        self._model_status = QLabel(self._default_status())
        self._model_status.setWordWrap(True)
        self._model_status.setEnabled(False)
        header.addWidget(self._model_status)
        return header

    def _default_status(self) -> str:
        if not engine.AVAILABLE:
            return f"Engine not installed. {engine.INSTALL_HINT}"
        return "No model selected."

    def _on_model_file_changed(self, path: str) -> None:
        path = (path or "").strip()
        if not path:
            self.state.set_manifest(None)
            self._settings_button.setEnabled(False)
            self._model_status.setText(self._default_status())
            return
        if not engine.AVAILABLE:
            self._model_status.setText(f"Engine not installed. {engine.INSTALL_HINT}")
            return
        try:
            manifest = engine.load_manifest(path)
        except Exception as exc:
            self.state.set_manifest(None)
            self._settings_button.setEnabled(False)
            self._model_status.setText(f"Invalid manifest: {exc}")
            return
        self.state.set_manifest(manifest, path)
        self._settings_button.setEnabled(bool(manifest.runtime))
        self._model_status.setText(
            f"Loaded {manifest.name} {manifest.version} — "
            f"{len(manifest.inputs)} inputs, {len(manifest.params)} parameters."
        )

    def _open_settings(self) -> None:
        manifest = self.state.manifest
        if manifest is not None and manifest.runtime:
            RuntimeSettingsDialog(manifest, self).exec()


def _placeholder_page(title: str) -> QWidget:
    page = QWidget()
    box = QVBoxLayout(page)
    box.addWidget(QLabel(f"<b>{title}</b>"))
    note = QLabel("Coming soon.")
    note.setEnabled(False)
    box.addWidget(note)
    box.addStretch(1)
    box.setAlignment(Qt.AlignmentFlag.AlignTop)
    return page
