"""The Run page: name the run, choose an output folder, launch, watch progress, load results.

This closes the deterministic single-run workflow. Parameters use the model's manifest
defaults (parameter editing arrives on the Parameters page); the run executes on a background
:class:`~almo_sweep_qgis.panels.run_task.RunTask` and its output layers are added to the
project when it finishes.
"""

from __future__ import annotations

import os
from typing import Any

from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import engine, settings
from almo_sweep_qgis.panels.run_task import RunTask
from almo_sweep_qgis.state import StudyState


class RunPage(QWidget):
    """Configure and launch a single deterministic run, then load its outputs."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._task: RunTask | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        form = QFormLayout()
        self._name_edit = QLineEdit("run_001")
        form.addRow("Run name", self._name_edit)

        self._out_dir = QgsFileWidget()
        self._out_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self._out_dir.setDialogTitle("Choose an output folder")
        form.addRow("Output folder", self._out_dir)
        root.addLayout(form)

        # Outputs to store: rebuilt from the manifest. Non-optional outputs are always kept
        # (checked and disabled); optional ones the user may switch off.
        self._outputs_group = QGroupBox("Outputs to store")
        self._outputs_layout = QVBoxLayout(self._outputs_group)
        self._output_checks: dict[str, QCheckBox] = {}
        root.addWidget(self._outputs_group)
        state.manifestChanged.connect(self._rebuild_outputs)
        self._rebuild_outputs(state.manifest)

        self._run_button = QPushButton("Run")
        self._run_button.clicked.connect(self._on_run_clicked)
        root.addWidget(self._run_button)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        root.addStretch(1)

    # -- launch ------------------------------------------------------------------------
    def _on_run_clicked(self) -> None:
        if not engine.AVAILABLE:
            self._set_status(f"Engine not installed. {engine.INSTALL_HINT}")
            return
        manifest = self._state.manifest
        if manifest is not None:
            missing = settings.missing_required(manifest)
            if missing:
                self._set_status(
                    "Configure the model's required settings first via 'Model settings…': "
                    + ", ".join(missing)
                )
                return
            missing_inputs = self._missing_inputs(manifest)
            if missing_inputs:
                self._set_status("Select the required input(s): " + ", ".join(missing_inputs))
                return
        try:
            runspec = self._build_runspec()
        except Exception as exc:
            self._set_status(f"Cannot start run: {exc}")
            return

        self._progress.setValue(0)
        self._run_button.setEnabled(False)
        self._set_status(f"Running '{runspec.run_id}' …")

        task = RunTask(self._state.manifest, runspec)
        task.progressChanged.connect(lambda pct: self._progress.setValue(int(pct)))
        task.taskCompleted.connect(self._on_completed)
        task.taskTerminated.connect(self._on_terminated)
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _missing_inputs(self, manifest: Any) -> list[str]:
        """Inputs that must be chosen for the current configuration but are not.

        An input is needed now if it is currently visible (its ``visible_when`` holds) and it
        is either always required or conditional (``visible_when`` implies "required when
        shown" — e.g. the release layer for a POLYGON release). Always-optional inputs never
        block a run.
        """
        values = self._state.param_values
        missing: list[str] = []
        for name, spec in manifest.inputs.items():
            if not spec.is_visible(values):
                continue
            required_now = spec.required or bool(spec.visible_when)
            if required_now and not self._state.input_layers.get(name):
                missing.append(spec.display_label)
        return missing

    def _build_runspec(self) -> Any:
        manifest = self._state.manifest
        if manifest is None:
            raise ValueError("no model selected")

        inputs: dict[str, str] = {}
        project = QgsProject.instance()
        for name, layer_id in self._state.input_layers.items():
            if not layer_id:
                continue
            layer = project.mapLayer(layer_id)
            if layer is not None:
                inputs[name] = layer.source()

        # Edited values from the Parameters page; resolve_runspec fills any gaps with
        # manifest defaults, so this works even if that page was never opened.
        params = dict(self._state.param_values)

        run_name = self._name_edit.text().strip() or "run"
        base = self._out_dir.filePath().strip()
        if not base:
            raise ValueError("choose an output folder")
        output_dir = os.path.join(base, run_name)

        return engine.resolve_runspec(
            manifest,
            run_id=run_name,
            inputs=inputs,
            params=params,
            output_dir=output_dir,
            store_outputs=self._selected_outputs(),
        )

    # -- output selection --------------------------------------------------------------
    def _rebuild_outputs(self, manifest: Any | None) -> None:
        while self._outputs_layout.count():
            item = self._outputs_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._output_checks.clear()

        outputs = manifest.outputs if manifest is not None else {}
        self._outputs_group.setVisible(bool(outputs))
        for name, spec in outputs.items():
            check = QCheckBox(spec.display_label)
            check.setChecked(True)
            check.setToolTip(spec.description or "")
            if not spec.optional:
                check.setEnabled(False)  # always stored
            self._outputs_layout.addWidget(check)
            self._output_checks[name] = check

    def _selected_outputs(self) -> list[str]:
        return [name for name, check in self._output_checks.items() if check.isChecked()]

    # -- completion (main thread) ------------------------------------------------------
    def _on_completed(self) -> None:
        self._run_button.setEnabled(True)
        result = self._task.result if self._task is not None else None
        self._task = None
        if result is None:
            self._set_status("Run finished, but produced no result.")
            return
        if result.ok:
            loaded = self._load_results(result)
            self._progress.setValue(100)
            self._set_status(f"Run '{result.run_id}' complete — loaded {loaded} output layer(s).")
        else:
            self._set_status(f"Run failed: {result.error}")

    def _on_terminated(self) -> None:
        self._run_button.setEnabled(True)
        message = self._task.error_message if self._task is not None else None
        self._task = None
        self._set_status(f"Run could not execute: {message or 'terminated'}")

    def _load_results(self, result: Any) -> int:
        manifest = self._state.manifest
        project = QgsProject.instance()
        loaded = 0
        for name, path in result.outputs.items():
            kind = None
            if manifest is not None and name in manifest.outputs:
                kind = manifest.outputs[name].kind
            if kind == "vector":
                layer: QgsRasterLayer | QgsVectorLayer = QgsVectorLayer(path, name, "ogr")
            else:
                layer = QgsRasterLayer(path, name)
            if layer.isValid():
                project.addMapLayer(layer)
                loaded += 1
        return loaded

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
