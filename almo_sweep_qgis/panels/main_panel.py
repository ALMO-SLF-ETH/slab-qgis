"""The main dockable panel: a model selector plus a navigator over the workflow sections.

The sections mirror the study workflow end to end:

1. **Parameters** -- the model's inputs *and* parameters, built from its manifest (the DEM and
   release-zone layers are chosen here, beside the parameters that select them).
2. **Study**      -- deterministic run or a probabilistic sweep (distributions, samples).
3. **Run**        -- name, output folder, output selection, launch and monitor.
4. **Results**    -- browse and inspect the runs produced.

The model is chosen at the top from a **persistent list** of registered models (stored in
:class:`QgsSettings`, so it survives restarts); "Add…" registers a new one from its manifest
and "Remove" drops it. Each model's runtime settings (e.g. ``DAMPM_BIN``/``DAMPM_DLL_DIR``)
are already persisted per model, so a re-selected model keeps its configured paths. Every page
reads the active model (and the user's selections) from a shared
:class:`~almo_sweep_qgis.state.StudyState`.
"""

from __future__ import annotations

from pathlib import Path

from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import engine, settings
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

        # A persistent list of models to pick from, plus buttons to add/remove one.
        self._model_combo = QComboBox()
        self._model_combo.setToolTip("Choose a registered model")
        self._model_combo.currentIndexChanged.connect(self._on_model_selected)
        row.addWidget(self._model_combo, 1)

        add_button = QToolButton()
        add_button.setText("Add…")
        add_button.setToolTip("Register a new model from its manifest (model.toml)")
        add_button.clicked.connect(self._on_add_model)
        row.addWidget(add_button, 0)

        self._remove_button = QToolButton()
        self._remove_button.setText("Remove")
        self._remove_button.setToolTip("Remove the selected model from the list")
        self._remove_button.clicked.connect(self._on_remove_model)
        row.addWidget(self._remove_button, 0)

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

        self._refresh_models(select=settings.last_model())
        return header

    def _default_status(self) -> str:
        if not engine.AVAILABLE:
            return f"Engine not installed. {engine.INSTALL_HINT}"
        return "No model selected. Use 'Add…' to register one."

    def _label_for_path(self, path: str) -> str:
        """A human label for a registered model: 'name version', or the filename if it won't
        load (e.g. a moved/deleted manifest)."""
        if engine.AVAILABLE:
            try:
                m = engine.load_manifest(path)
                return f"{m.name} {m.version}"
            except Exception:
                return f"{Path(path).name} (invalid)"
        return Path(path).name

    def _refresh_models(self, select: str = "") -> None:
        """Rebuild the combo from the registry, then select ``select`` (by path) if present."""
        models = settings.registered_models()
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for path in models:
            self._model_combo.addItem(self._label_for_path(path), path)
            self._model_combo.setItemData(
                self._model_combo.count() - 1, path, Qt.ItemDataRole.ToolTipRole
            )
        target = select if select in models else (models[0] if models else "")
        index = models.index(target) if target in models else -1
        self._model_combo.setCurrentIndex(index)
        self._model_combo.blockSignals(False)
        self._remove_button.setEnabled(bool(models))
        # setCurrentIndex above was signal-blocked, so drive the load explicitly.
        self._load_current_model()

    def _load_current_model(self) -> None:
        path = self._model_combo.currentData()
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
        settings.set_last_model(path)
        self._settings_button.setEnabled(bool(manifest.runtime))
        self._model_status.setText(
            f"Loaded {manifest.name} {manifest.version} — "
            f"{len(manifest.inputs)} inputs, {len(manifest.params)} parameters."
        )

    def _on_model_selected(self, _index: int) -> None:
        self._load_current_model()

    def _on_add_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select a model manifest (model.toml)",
            "",
            "Model manifest (*.toml);;All files (*)",
        )
        if not path:
            return
        if engine.AVAILABLE:
            try:
                engine.load_manifest(path)  # validate before registering
            except Exception as exc:
                self._model_status.setText(f"Not added — invalid manifest: {exc}")
                return
        settings.add_model(path)
        self._refresh_models(select=path)

    def _on_remove_model(self) -> None:
        path = self._model_combo.currentData()
        if path:
            settings.remove_model(path)
            self._refresh_models(select=settings.last_model())

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
