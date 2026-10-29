"""The Run page: name the run, choose an output folder, launch, watch progress, load results.

This closes the deterministic single-run workflow. Parameters use the model's manifest
defaults (parameter editing arrives on the Parameters page); the run executes on a background
:class:`~almo_sweep_qgis.panels.run_task.RunTask` and its output layers are added to the
project when it finishes.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import shutil
from html import escape
from pathlib import Path
from typing import Any, ClassVar

from qgis.core import (
    QgsApplication,
    QgsBilinearRasterResampler,
    QgsCoordinateTransformContext,
    QgsDataProvider,
    QgsDateTimeRange,
    QgsGradientColorRamp,
    QgsProject,
    QgsRasterFileWriter,
    QgsRasterLayer,
    QgsRasterLayerTemporalProperties,
    QgsRasterPipe,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import QDate, QDateTime, QTime, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import engine, settings, sweep_plan
from almo_sweep_qgis.dem.resample import default_resampled_path, raster_resolution
from almo_sweep_qgis.dem.style import apply_hillshade
from almo_sweep_qgis.panels.run_pipeline import ResampleTask, RunController
from almo_sweep_qgis.panels.run_task import RunTask, SweepTask
from almo_sweep_qgis.state import StudyState


class RunPage(QWidget):
    """Configure and launch a single deterministic run, then load its outputs."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._task: RunTask | None = None
        self._controller: RunController | None = None
        self._loaded_frame_series = False
        self._is_sweep = False  # whether the launched batch is a parameter sweep
        self._project_root_path: str | None = None  # the current run's project folder (summary)
        #: input name -> path to feed the run(s), overriding the layer's own source. Set by the
        #: copy-inputs step (and left empty when inputs are used in place).
        self._inputs_override: dict[str, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        form = QFormLayout()
        self._out_dir = QgsFileWidget()
        self._out_dir.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self._out_dir.setDialogTitle("Choose an output folder")
        form.addRow("Output folder", self._out_dir)

        self._name_edit = QLineEdit("project")
        self._name_edit.setToolTip(
            "A folder with this name is created in the output folder; each run goes in its own "
            "sub-folder (<project>_000001, …)."
        )
        form.addRow("Project name", self._name_edit)

        self._copy_inputs_check = QCheckBox("Copy input layers into the project (inputs/)")
        self._copy_inputs_check.setChecked(True)
        self._copy_inputs_check.setToolTip(
            "Copy the DEM and release layers into <project>/inputs/ so the project is "
            "self-contained. Inputs already inside the project are left in place."
        )
        form.addRow("", self._copy_inputs_check)
        root.addLayout(form)

        # Study execution: how many runs execute at once, and how many times a failed run retries.
        # Only relevant to a sweep, so the group is shown only while one is enabled.
        self._exec_group = QGroupBox("Study execution")
        exec_form = QFormLayout(self._exec_group)
        self._workers_spin = QSpinBox()
        cpu_cap = engine.default_max_workers() if engine.AVAILABLE else 1
        cpu_cap = max(1, cpu_cap)
        self._workers_spin.setRange(1, cpu_cap)
        self._workers_spin.setValue(cpu_cap)  # default to all logical processors
        self._workers_spin.setToolTip(
            "How many simulations run at once (default: this machine's processor count). Each run "
            "is a single-threaded process, so this many run truly in parallel. For strongly "
            "CPU-bound work the physical core count (often half of this) can be a touch faster — "
            "lower it to taste."
        )
        # Show the ceiling next to the field so the user knows the most they can ask for.
        workers_row = QHBoxLayout()
        workers_row.setContentsMargins(0, 0, 0, 0)
        workers_row.addWidget(self._workers_spin)
        max_label = QLabel(f"of {cpu_cap} available (processor count)")
        max_label.setEnabled(False)  # muted hint
        workers_row.addWidget(max_label)
        workers_row.addStretch(1)
        workers_container = QWidget()
        workers_container.setLayout(workers_row)
        exec_form.addRow("Parallel runs", workers_container)
        self._retries_spin = QSpinBox()
        self._retries_spin.setRange(0, 5)
        self._retries_spin.setValue(0)
        self._retries_spin.setToolTip("Re-attempt a failed run this many times before giving up.")
        exec_form.addRow("Retries per run", self._retries_spin)
        root.addWidget(self._exec_group)
        state.sweepChanged.connect(self._sync_exec_group)
        state.manifestChanged.connect(lambda _m: self._sync_exec_group())
        self._sync_exec_group()

        # Outputs to store: rebuilt from the manifest. Non-optional outputs are always kept
        # (checked and disabled); optional ones the user may switch off.
        self._outputs_group = QGroupBox("Outputs to store")
        self._outputs_layout = QVBoxLayout(self._outputs_group)
        self._output_checks: dict[str, QCheckBox] = {}
        root.addWidget(self._outputs_group)
        state.manifestChanged.connect(self._rebuild_outputs)
        self._rebuild_outputs(state.manifest)

        buttons = QHBoxLayout()
        self._run_button = QPushButton("Run")
        self._run_button.clicked.connect(self._on_run_clicked)
        buttons.addWidget(self._run_button)
        self._stop_button = QPushButton("Stop")
        self._stop_button.setToolTip("Terminate all running simulations")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._on_stop_clicked)
        buttons.addWidget(self._stop_button)
        root.addLayout(buttons)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        # Current phase + time estimate, e.g. "Resampling 'dem' to 2 m — 0:05 elapsed · ~0:20 left".
        self._progress_info = QLabel("")
        self._progress_info.setWordWrap(True)
        self._progress_info.setEnabled(False)
        root.addWidget(self._progress_info)

        # Rich, scrollable status/log: one line per message, each tagged with a status icon, and
        # clickable "open log" links for failed runs. We handle clicks ourselves (setOpenLinks off)
        # so a run log opens *inside* this view with a link back to the study summary, rather than
        # the browser navigating away to the file with no way back.
        self._status = QTextBrowser()
        self._status.setOpenLinks(False)
        self._status.anchorClicked.connect(self._on_status_anchor)
        self._status.setMinimumHeight(90)
        root.addWidget(self._status, 1)
        self._status_html = ""  # the current study/run summary, restored by the "back" link
        self._current_log: str | None = None

    # -- launch ------------------------------------------------------------------------
    def _on_run_clicked(self) -> None:
        if not engine.AVAILABLE:
            self._set_status(f"Engine not installed. {engine.INSTALL_HINT}", level="warn")
            return
        manifest = self._state.manifest
        if manifest is not None:
            missing = settings.missing_required(manifest)
            if missing:
                self._set_status(
                    "Configure the model's required settings first via 'Model settings…': "
                    + ", ".join(missing),
                    level="warn",
                )
                return
            missing_inputs = self._missing_inputs(manifest)
            if missing_inputs:
                self._set_status(
                    "Select the required input(s): " + ", ".join(missing_inputs),
                    level="warn",
                )
                return
            being_edited = self._inputs_being_edited(manifest)
            if being_edited:
                # The run reads each layer's file on disk, not its in-memory edit buffer, so an
                # input left in edit mode (or with unsaved edits) would simulate geometry that no
                # longer matches the screen. Block until the user commits and leaves edit mode.
                self._set_status(
                    "Finish editing and save the layer(s) before running — they have open edits "
                    "that are not on disk yet, so the run would use the old geometry: "
                    + ", ".join(being_edited),
                    level="warn",
                )
                return
            # The model reads each input from a file path, so any input that is only a temporary /
            # in-memory / project-embedded layer (no file on disk) must be saved first.
            if not self._ensure_inputs_saved(manifest):
                return

        try:
            project_root = self._project_root()  # <output folder>/<project>
        except ValueError as exc:
            self._set_status(str(exc), level="warn")
            return
        self._project_root_path = project_root
        self._inputs_override = {}

        # Build the batch of background steps, in order:
        #   1. any armed raster resamples (re-point inputs to the resampled rasters),
        #   2. optionally copy the inputs into <project>/inputs/ (runs after resamples so it
        #      copies the resampled rasters),
        #   3. the run(s): one for a deterministic study, one per sample for a sweep.
        # The pipeline treats them all the same -- the progress bar/ETA describe the whole batch.
        jobs: list[Any] = self._pending_resample_jobs(manifest)
        if self._copy_inputs_check.isChecked() and manifest is not None and manifest.inputs:
            jobs.append(_CopyInputsJob(self, manifest, os.path.join(project_root, "inputs")))

        sweep = dict(getattr(self._state, "sweep", {}) or {})
        self._is_sweep = bool(sweep.get("enabled")) and manifest is not None
        if self._is_sweep:
            try:
                jobs.extend(self._build_sweep_jobs(manifest, sweep, project_root))
            except Exception as exc:
                self._set_status(f"Cannot start the sweep: {exc}", level="error")
                return
        else:
            run_id = self._run_id(1, 1)
            jobs.append(
                _RunJob(
                    self,
                    sample={
                        "run_id": run_id,
                        "params": dict(self._state.param_values),
                        "output_dir": os.path.join(project_root, run_id),
                        "weight": 1.0,
                    },
                )
            )

        self._progress.setValue(0)
        self._run_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._set_status("Starting…")

        controller = RunController(jobs)
        controller.progressChanged.connect(self._on_pipeline_progress)
        controller.finished.connect(self._on_pipeline_finished)
        controller.failed.connect(self._on_pipeline_failed)
        controller.stopped.connect(self._on_pipeline_stopped)
        self._controller = controller
        controller.start()

    # -- stop --------------------------------------------------------------------------
    def _on_stop_clicked(self) -> None:
        """Cancel every running simulation; each cancel kills its model subprocess.

        Cancelling a :class:`RunTask` sets its ``isCanceled`` flag, which the engine polls and
        acts on by terminating the model process — so the run actually stops rather than finishing
        in the background. The task's ``taskTerminated`` signal then updates the UI.
        """
        tasks = self._running_tasks()
        if self._controller is None and not tasks:
            self._stop_button.setEnabled(False)
            self._set_status("No simulation is running.")
            return
        self._stop_button.setEnabled(False)  # one stop is enough; avoid repeat clicks
        self._set_status("Stopping — terminating the running step…")
        # Cancel the active pipeline step (resample or run); also sweep the task manager for any
        # RunTask, so a stray run from elsewhere is caught too.
        if self._controller is not None:
            with contextlib.suppress(Exception):
                self._controller.cancel()
        for task in tasks:
            with contextlib.suppress(Exception):
                task.cancel()

    def _running_tasks(self) -> list[RunTask]:
        """All active ALMO run tasks (this page's run plus any other RunTask the manager holds)."""
        tasks: list[RunTask] = []
        with contextlib.suppress(Exception):
            for task in QgsApplication.taskManager().tasks():
                if isinstance(task, RunTask) and task not in tasks:
                    tasks.append(task)
        if self._task is not None and self._task not in tasks:
            tasks.append(self._task)
        return tasks

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

    def _inputs_being_edited(self, manifest: Any) -> list[str]:
        """Labels of currently-visible input layers that have uncommitted edits.

        A vector layer in edit mode, or with an unsaved edit buffer, differs from its file on
        disk — and the engine reads the file. Such a layer must be committed and closed before a
        run so the simulation uses the geometry shown on the canvas.
        """
        values = self._state.param_values
        project = QgsProject.instance()
        editing: list[str] = []
        for name, spec in manifest.inputs.items():
            if not spec.is_visible(values):
                continue
            layer_id = self._state.input_layers.get(name)
            if not layer_id:
                continue
            layer = project.mapLayer(layer_id)
            if isinstance(layer, QgsVectorLayer) and (layer.isEditable() or layer.isModified()):
                editing.append(spec.display_label)
        return editing

    def _ensure_inputs_saved(self, manifest: Any) -> bool:
        """Make sure every visible input layer is backed by a file on disk before running.

        A layer that is only in memory, temporary, or embedded in the project with no real file
        path (so the model process cannot open it) is offered a "save to disk" dialog; the layer is
        exported and re-pointed at that file. Returns True when all inputs are on disk, False if the
        user cancelled saving one (the run is then aborted).
        """
        values = self._state.param_values
        project = QgsProject.instance()
        for name, spec in manifest.inputs.items():
            if not spec.is_visible(values):
                continue
            layer_id = self._state.input_layers.get(name)
            if not layer_id:
                continue
            layer = project.mapLayer(layer_id)
            if layer is None or _layer_local_path(layer) is not None:
                continue  # not selected, or already a file on disk
            if not self._prompt_and_save_layer(spec.display_label, layer):
                self._set_status(
                    f"Run cancelled: the input '{spec.display_label}' is not saved to disk yet. "
                    "Save it (or pick a file-based layer) and run again."
                )
                return False
        return True

    def _pending_resample_jobs(self, manifest: Any) -> list[Any]:
        """Build a resample job for each raster input whose "Resample to" is armed and needed.

        Each job resamples the input's raster to the requested resolution in the background and, on
        success, re-points the input at the result. Inputs that are not in use, not on disk, or
        already at the target resolution (e.g. Apply was pressed) are skipped -- so pressing Apply
        first costs nothing.
        """
        jobs: list[Any] = []
        requests = getattr(self._state, "resample_requests", None)
        if not requests or manifest is None:
            return jobs
        project = QgsProject.instance()
        values = self._state.param_values
        for name, req in list(requests.items()):
            spec = manifest.inputs.get(name)
            if spec is not None and not spec.is_visible(values):
                continue  # this input is not used in the current configuration
            layer_id = self._state.input_layers.get(name)
            if not layer_id:
                continue
            layer = project.mapLayer(layer_id)
            if not isinstance(layer, QgsRasterLayer):
                continue
            src = _layer_local_path(layer)
            if src is None:
                continue  # not on disk (ensure_inputs_saved runs first, so this is defensive)
            res = float(req.get("resolution") or 0.0)
            if res <= 0:
                continue
            native = raster_resolution(src)
            if native is not None and abs(native[0] - res) < 1e-9 and abs(native[1] - res) < 1e-9:
                continue  # already at the target resolution (nothing to do)
            dst = (req.get("out_path") or "").strip() or default_resampled_path(src, res)
            jobs.append(_ResampleJob(self, name, src, dst, res, req.get("alg") or "bilinear"))
        return jobs

    def _load_resampled_input(self, name: str, path: str) -> None:
        """Main-thread: load a freshly-resampled raster and re-point input ``name`` at it."""
        new_layer = QgsRasterLayer(path, os.path.basename(path))
        if not new_layer.isValid():
            raise RuntimeError(f"the resampled raster could not be loaded: {path}")
        QgsProject.instance().addMapLayer(new_layer, True)
        apply_hillshade(new_layer)
        self._state.input_layers[name] = new_layer.id()
        self._state.inputLayerReplaced.emit(name, new_layer.id())

    def _prompt_and_save_layer(self, label: str, layer: Any) -> bool:
        """Ask where to save ``layer``, export it, and re-point it. False if the user cancelled."""
        is_raster = isinstance(layer, QgsRasterLayer)
        suggested = self._suggested_save_path(layer, is_raster)
        if is_raster:
            path, _ = QFileDialog.getSaveFileName(
                self, f"Save '{label}' to disk (not stored yet)", suggested, "GeoTIFF (*.tif)"
            )
        else:
            path, _ = QFileDialog.getSaveFileName(
                self,
                f"Save '{label}' to disk (not stored yet)",
                suggested,
                "GeoPackage (*.gpkg);;Shapefile (*.shp)",
            )
        if not path:
            return False
        try:
            saved = _save_raster(layer, path) if is_raster else _save_vector(layer, path)
        except Exception as exc:
            self._set_status(f"Could not save '{label}': {exc}", level="error")
            return False
        # Re-point the layer at the new file so the run (and future runs) read it from disk.
        provider = "gdal" if is_raster else "ogr"
        layer.setDataSource(saved, layer.name(), provider, QgsDataProvider.ProviderOptions())
        return True

    def _suggested_save_path(self, layer: Any, is_raster: bool) -> str:
        base = self._out_dir.filePath().strip() or os.path.expanduser("~")
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in layer.name()) or "layer"
        return os.path.join(base, name + (".tif" if is_raster else ".gpkg"))

    # -- project / run folder layout ---------------------------------------------------
    def _project_name(self) -> str:
        return self._name_edit.text().strip() or "project"

    def _project_root(self) -> str:
        """``<output folder>/<project>`` — the folder holding ``inputs/`` and the run folders."""
        base = self._out_dir.filePath().strip()
        if not base:
            raise ValueError("choose an output folder")
        return os.path.join(base, self._project_name())

    def _run_id(self, index: int, total: int) -> str:
        """``<project>_000001`` — zero-padded to at least 6 digits (more if the sweep is larger)."""
        width = max(6, len(str(total)))
        return f"{self._project_name()}_{index:0{width}d}"

    def _build_runspec(
        self,
        *,
        run_id: str | None = None,
        params: dict[str, Any] | None = None,
        output_dir: str | None = None,
    ) -> Any:
        """Resolve a run-spec. With no arguments, the single deterministic run; a sweep passes a
        per-sample ``run_id``/``params``/``output_dir``. Inputs (and their CRS) are shared; a path
        in ``self._inputs_override`` (set by the copy-inputs step) replaces a layer's own source."""
        manifest = self._state.manifest
        if manifest is None:
            raise ValueError("no model selected")

        inputs: dict[str, str] = {}
        input_crs: dict[str, str] = {}
        project = QgsProject.instance()
        for name, layer_id in self._state.input_layers.items():
            if not layer_id:
                continue
            layer = project.mapLayer(layer_id)
            if layer is not None:
                inputs[name] = self._inputs_override.get(name) or layer.source()
                # Capture the layer's CRS so the model can georeference its outputs. An ESRI ASCII
                # DEM carries no CRS inline, so this is the only reliable source. Prefer the compact
                # authority id (e.g. "EPSG:2056"); fall back to WKT for a custom CRS.
                crs = layer.crs()
                if crs is not None and crs.isValid():
                    input_crs[name] = crs.authid() or crs.toWkt()

        # Edited values from the Parameters page (or a sweep sample); resolve_runspec fills any
        # gaps with manifest defaults, so this works even if the Parameters page was never opened.
        params = dict(params) if params is not None else dict(self._state.param_values)

        run_id = run_id or self._run_id(1, 1)
        if output_dir is None:
            output_dir = os.path.join(self._project_root(), run_id)

        return engine.resolve_runspec(
            manifest,
            run_id=run_id,
            inputs=inputs,
            params=params,
            output_dir=output_dir,
            store_outputs=self._selected_outputs(),
            input_crs=input_crs,
        )

    # -- copy inputs into the project --------------------------------------------------
    def _inputs_copy_plan(self, manifest: Any, inputs_dir: str) -> list[tuple[str, str]]:
        """Which inputs to copy into ``inputs_dir``: ``(name, source_path)`` for every visible
        input that is on disk and not already inside the project folder."""
        project_root = self._project_root()
        project = QgsProject.instance()
        values = self._state.param_values
        plan: list[tuple[str, str]] = []
        for name, spec in manifest.inputs.items():
            if not spec.is_visible(values):
                continue
            layer_id = self._state.input_layers.get(name)
            if not layer_id:
                continue
            layer = project.mapLayer(layer_id)
            if layer is None:
                continue
            src = _layer_local_path(layer)
            if src is None or _is_within(src, project_root):
                continue  # not on disk, or already inside the project (nothing to copy)
            plan.append((name, src))
        return plan

    def _resolve_inputs_copy_plan(
        self, manifest: Any, inputs_dir: str
    ) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
        """Decide how each input is placed in ``inputs/``, verifying identity by name + checksum.

        For every input that would be copied: if no file of that name is there yet, it is copied;
        if a file of that name is there and its **checksum matches**, it is reused in place (no
        copy); if the name matches but the **data differs**, the user is asked to either copy the
        selected file under a new name (and run with it) or cancel the run to inspect the files.

        Returns ``(copies, reuse)`` where ``copies`` is ``(name, src, dst)`` operations to perform
        and ``reuse`` maps an input name to an already-present identical file. Raises
        ``RuntimeError`` if the user cancels over a conflict."""
        copies: list[tuple[str, str, str]] = []
        reuse: dict[str, str] = {}
        for name, src in self._inputs_copy_plan(manifest, inputs_dir):
            dst = os.path.join(inputs_dir, os.path.basename(src))
            if not os.path.exists(dst):
                copies.append((name, src, dst))
            elif _same_file(src, dst):
                reuse[name] = dst  # identical data already in the project — reuse it
            elif self._prompt_input_conflict(os.path.basename(src), src, dst) == "rename":
                copies.append((name, src, _unique_dst(inputs_dir, src)))
            else:
                raise RuntimeError(
                    f"run cancelled: an input named '{os.path.basename(src)}' already exists in "
                    f"'{inputs_dir}' with different contents — inspect the files, then run again"
                )
        return copies, reuse

    def _prompt_input_conflict(self, name: str, src: str, dst: str) -> str:
        """Ask the user how to resolve a same-name/different-data input conflict.

        Returns ``"rename"`` (copy the selected file under a new name and run with it) or
        ``"cancel"`` (abort so the files can be inspected)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Input file conflict")
        box.setText(
            f"An input named '{name}' already exists in the project's inputs folder, but its "
            f"contents differ from the file you selected."
        )
        box.setInformativeText(
            f"Existing: {dst}\nSelected: {src}\n\nCopy the selected file under a new name and run "
            f"with it, or cancel the run to inspect the files yourself?"
        )
        rename_btn = box.addButton("Copy under a new name", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel run", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "rename" if box.clickedButton() is rename_btn else "cancel"

    # -- sweep ------------------------------------------------------------------------
    def _build_sweep_jobs(
        self, manifest: Any, sweep: dict[str, Any], project_root: str
    ) -> list[Any]:
        """Expand the study into a single scheduler-driven job, and write the study plan.

        Every sample gets its own run id and output sub-folder (``<project>/<project>_NNNNNN``);
        the whole batch runs through the engine scheduler (one :class:`_SweepJob`) with the
        Parallel-runs / Retries settings. ``study.json`` records the intended plan (scheme, seed and
        each sample's parameters and weight); the engine writes ``runs.json`` as runs complete,
        cataloguing their outcomes and weights for a later probability-weighted map.
        """
        plan = sweep_plan.build_plan(manifest, sweep, dict(self._state.param_values))
        if not plan.samples:
            raise ValueError("the sweep produced no samples — select a parameter to vary")

        os.makedirs(project_root, exist_ok=True)
        samples: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        for i, sample in enumerate(plan.samples, start=1):
            run_id = self._run_id(i, plan.count)
            values = dict(sample.values)
            samples.append(
                {
                    "run_id": run_id,
                    "params": values,
                    "output_dir": os.path.join(project_root, run_id),
                    "weight": sample.weight,
                }
            )
            records.append({"run_id": run_id, "weight": sample.weight, "params": values})

        self._write_study_json(project_root, self._project_name(), plan, records, sweep)
        index = engine.default_index(
            project_root, manifest, scheme=plan.describe, seed=plan.seed, count=plan.count
        )
        return [
            _SweepJob(
                self,
                manifest,
                samples,
                index,
                max_workers=self._workers_spin.value(),
                retries=self._retries_spin.value(),
            )
        ]

    @staticmethod
    def _write_study_json(
        project_root: str,
        project_name: str,
        plan: Any,
        records: list[dict[str, Any]],
        sweep: dict[str, Any],
    ) -> None:
        data = {
            "project": project_name,
            "scheme": plan.describe,
            "scheme_key": sweep.get("scheme"),
            "seed": plan.seed,
            "count": plan.count,
            # The per-parameter distributions chosen for the sweep, so the Results page can report
            # "friction ~ Normal(25, 3)" without re-deriving it from the sampled values.
            "swept": dict(sweep.get("params") or {}),
            "samples": records,
        }
        path = os.path.join(project_root, "study.json")
        with contextlib.suppress(OSError), open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

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

    def _sync_exec_group(self) -> None:
        """Show the parallel-runs / retries controls only while a sweep is enabled."""
        sweep = getattr(self._state, "sweep", {}) or {}
        self._exec_group.setVisible(bool(sweep.get("enabled")))

    # -- pipeline (main thread) --------------------------------------------------------
    def _on_pipeline_progress(self, fraction: float, info: str) -> None:
        self._progress.setValue(round(fraction * 100.0))
        self._progress_info.setText(info)

    def _on_pipeline_finished(self, results: Any) -> None:
        self._controller = None
        self._run_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._progress_info.setText("")
        # A sweep job contributes a *list* of results (one per sample); a single run contributes one
        # RunResult. Flatten either shape into one list of results.
        run_results: list[Any] = []
        for item in results or []:
            if isinstance(item, list):
                run_results.extend(r for r in item if r is not None)
            elif item is not None:
                run_results.append(item)
        if not run_results:
            self._set_status("Run finished, but produced no result.", level="warn")
            return
        # Point the Results page at the project that was just simulated.
        with contextlib.suppress(Exception):
            self._state.projectFinished.emit(self._project_root())
        if self._is_sweep:
            self._report_sweep_finished(run_results)
            return
        # A single deterministic run: load its outputs onto the map.
        lines: list[tuple[str, str]] = []
        any_ok = False
        for result in run_results:
            if result.ok:
                any_ok = True
                loaded = self._load_results(result)
                lines.append(
                    (
                        "ok",
                        f"Run <b>{escape(result.run_id)}</b> complete — {loaded} layer(s) added, "
                        f"grouped under '{escape(result.run_id)}'.",
                    )
                )
            else:
                lines.append(("error", self._failure_html(result)))
        if any_ok:
            self._progress.setValue(100)
            if self._loaded_frame_series:
                lines.append(
                    (
                        "info",
                        "Open View ▸ Panels ▸ Temporal Controller (animation mode) to step "
                        "through the frames.",
                    )
                )
        self._render_status(lines)

    def _report_sweep_finished(self, run_results: list[Any]) -> None:
        """Summarise a finished sweep without loading every run's layers.

        A sweep can be hundreds of runs, each with several output layers, so auto-loading them all
        would overwhelm the project; inspection and aggregation belong to the Results page. Report
        how many succeeded and where the outputs are, and surface the first few failures.
        """
        self._progress.setValue(100)
        ok = sum(1 for r in run_results if r.ok)
        failed = [r for r in run_results if not r.ok]
        total = len(run_results)
        lines: list[tuple[str, str]] = []
        if failed:
            lines.append(
                (
                    "warn",
                    f"<b>Sweep complete</b> — {ok} of {total} succeeded, {len(failed)} failed.",
                )
            )
        else:
            lines.append(("ok", f"<b>Sweep complete</b> — all {total} simulation(s) succeeded."))
        if self._project_root_path:
            lines.append(
                (
                    "info",
                    f"Outputs in <code>{escape(self._project_root_path)}</code>. "
                    "Use the Results page to inspect and aggregate them.",
                )
            )
        for r in failed:
            lines.append(("error", self._failure_html(r)))
        self._render_status(lines)

    def _failure_html(self, result: Any) -> str:
        """One failed run as HTML: run id, error, and a clickable 'open log' link when present."""
        err = str(result.error or "failed")
        log_path: str | None = None
        if "Full log:" in err:
            err, log_path = (part.strip() for part in err.split("Full log:", 1))
            err = err.rstrip(".")
        html = f"<b>{escape(result.run_id)}</b> — {escape(err)}"
        if log_path and os.path.exists(log_path):
            href = QUrl.fromLocalFile(log_path).toString()
            html += f' · <a href="{href}">open log</a>'
        return html

    def _on_pipeline_failed(self, message: str) -> None:
        self._controller = None
        self._run_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._progress_info.setText("")
        self._set_status(f"Run could not execute: {message or 'terminated'}", level="error")

    def _on_pipeline_stopped(self) -> None:
        self._controller = None
        self._run_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._progress.setValue(0)
        self._progress_info.setText("")
        self._set_status("Run stopped.", level="warn")

    def _load_results(self, result: Any) -> int:
        """Load a run's outputs into one layer-tree group named after the run.

        Inside that group: ``grid_series`` as temporal subgroups (stepable in time), and the
        multi-band rasters (``final_grid``, ``max_values``) each unpacked into one single-band
        pseudocolour layer per band, under their own subgroup.
        """
        manifest = self._state.manifest
        project = QgsProject.instance()
        self._loaded_frame_series = False
        outputs = dict(result.outputs)
        max_values_path = outputs.get("max_values")

        run_group = project.layerTreeRoot().insertGroup(0, str(result.run_id))
        loaded = 0
        for name, path in outputs.items():
            if os.path.isdir(path):
                # A directory of raster frames -> temporal subgroup (e.g. dampm's grid_series).
                loaded += self._load_frame_series(name, path, max_values_path, run_group)
                continue
            kind = manifest.outputs[name].kind if (manifest and name in manifest.outputs) else None
            if kind == "vector":
                vlayer = QgsVectorLayer(path, name, "ogr")
                if vlayer.isValid():
                    project.addMapLayer(vlayer, False)
                    run_group.addLayer(vlayer)
                    loaded += 1
                continue
            loaded += self._load_banded_raster(name, path, run_group)

        if loaded == 0:
            project.layerTreeRoot().removeChildNode(run_group)  # nothing loaded -> no empty group
        return loaded

    def _load_banded_raster(self, name: str, path: str, run_group: Any) -> int:
        """Unpack a multi-band raster into one single-band pseudocolour layer per band.

        The per-band layers go in a subgroup named after the output (e.g. ``final_grid`` with its
        h / v_2D / mass / v_x / v_y / v_z bands, ``max_values`` with h_max / v_2D_max). Band names
        come from the file's band descriptions when present.
        """
        probe = QgsRasterLayer(path, name)
        if not probe.isValid():
            return 0
        project = QgsProject.instance()
        ramp = self._turbo_ramp()
        subgroup = run_group.addGroup(name)
        count = 0
        for band in range(1, probe.bandCount() + 1):
            layer = QgsRasterLayer(path, f"{name} — {probe.bandName(band)}")
            if not layer.isValid():
                continue
            self._style_singleband(layer, band, ramp)
            project.addMapLayer(layer, False)
            subgroup.addLayer(layer)
            count += 1
        if count == 0:
            run_group.removeChildNode(subgroup)
        return count

    def _style_singleband(
        self, layer: QgsRasterLayer, band: int, ramp: Any, upper: float | None = None
    ) -> None:
        """Render ``band`` as single-band pseudocolour (Turbo) with bilinear display resampling."""
        dp = layer.dataProvider()
        dp.setNoDataValue(band, -9999.0)  # dampm's nodata sentinel
        renderer = QgsSingleBandPseudoColorRenderer(dp, band)
        lo, hi = 0.0, upper
        if hi is None:
            with contextlib.suppress(Exception):
                stats = dp.bandStatistics(band)
                lo, hi = stats.minimumValue, stats.maximumValue
        renderer.setClassificationMin(float(lo))
        if hi is not None and hi > lo:
            renderer.setClassificationMax(float(hi))
        renderer.createShader(ramp.clone())
        layer.setRenderer(renderer)
        resampler = layer.resampleFilter()
        if resampler is not None:
            resampler.setZoomedInResampler(QgsBilinearRasterResampler())
            resampler.setZoomedOutResampler(QgsBilinearRasterResampler())

    #: Frame-raster bands to present as temporal groups: (band index, subgroup label). Matches
    #: dampm's grid rasters (band 1 = flow height h, band 2 = 2D speed v).
    _FRAME_BANDS = ((1, "flow height h"), (2, "velocity v_2D"))

    def _load_frame_series(
        self, name: str, folder: str, max_values_path: str | None, run_group: Any
    ) -> int:
        """Load a folder of raster frames as temporal subgroups (inside ``run_group``).

        A ``name`` subgroup (e.g. ``grid_series``) holds one subgroup per band in
        :attr:`_FRAME_BANDS`, each frame a timestamp (spaced by the model's output interval).
        Frames are single-band pseudocolour (**Turbo**); the per-band colour upper bound is taken
        from ``max_values.tif`` (the run maxima), so colours are consistent across frames and equal
        to the run's true maximum. Times map onto a dummy datetime axis (QGIS has no 'elapsed
        seconds' axis); spacing uses ``output_dt`` from the parameters when exposed, else 1/frame.
        """
        frames = sorted(glob.glob(os.path.join(folder, "*.tif")))
        if len(frames) < 2:
            return 0  # not a stepable series

        project = QgsProject.instance()
        spacing_s = float(self._state.param_values.get("output_dt", 1.0) or 1.0)
        base = QDateTime(QDate(2000, 1, 1), QTime(0, 0, 0))
        mode = (
            getattr(QgsRasterLayerTemporalProperties, "ModeFixedTemporalRange", None)
            or QgsRasterLayerTemporalProperties.TemporalMode.ModeFixedTemporalRange
        )
        ramp = self._turbo_ramp()
        uppers = self._band_upper_bounds(max_values_path, [b for b, _ in self._FRAME_BANDS])

        parent = run_group.addGroup(name)
        total = 0
        for band, blabel in self._FRAME_BANDS:
            # upper colour bound: from max_values.tif, else the frames' own max for this band
            upper = uppers.get(band)
            if upper is None:
                upper = self._frames_band_max(frames, band)
            subgroup = parent.addGroup(blabel)
            for i, path in enumerate(frames):
                layer = QgsRasterLayer(path, f"t = {i * spacing_s:g} s")
                if not layer.isValid() or layer.bandCount() < band:
                    continue
                self._style_singleband(layer, band, ramp, upper if upper and upper > 0 else None)

                tp = layer.temporalProperties()
                tp.setIsActive(True)
                tp.setMode(mode)
                start = base.addMSecs(int(i * spacing_s * 1000))
                end = base.addMSecs(int((i + 1) * spacing_s * 1000))
                # Half-open [start, end): adjacent frames must not share the boundary instant,
                # otherwise both render at that timestamp. (includeBeginning=True, includeEnd=False)
                tp.setFixedTemporalRange(QgsDateTimeRange(start, end, True, False))

                project.addMapLayer(layer, False)
                subgroup.addLayer(layer)
                total += 1

        if total:
            self._loaded_frame_series = True
        else:
            run_group.removeChildNode(parent)
        return total

    def _band_upper_bounds(self, max_values_path: str | None, bands: list[int]) -> dict[int, float]:
        """Per-band colour upper bounds from ``max_values.tif`` (band i max = run maximum)."""
        bounds: dict[int, float] = {}
        if not max_values_path or not os.path.isfile(max_values_path):
            return bounds
        mv = QgsRasterLayer(max_values_path, "max_values")
        if not mv.isValid():
            return bounds
        dp = mv.dataProvider()
        for band in bands:
            if band <= dp.bandCount():
                with contextlib.suppress(Exception):
                    bounds[band] = dp.bandStatistics(band).maximumValue
        return bounds

    @staticmethod
    def _frames_band_max(frames: list[str], band: int) -> float | None:
        """Largest value of ``band`` across all frames (fallback when max_values is absent)."""
        result: float | None = None
        for path in frames:
            layer = QgsRasterLayer(path, "f")
            if not layer.isValid() or layer.bandCount() < band:
                continue
            with contextlib.suppress(Exception):
                m = layer.dataProvider().bandStatistics(band).maximumValue
                result = m if result is None else max(result, m)
        return result

    @staticmethod
    def _turbo_ramp() -> Any:
        """The Turbo colour ramp (falling back to other perceptual ramps, then a gradient)."""
        style = QgsStyle.defaultStyle()
        for n in ("Turbo", "Viridis", "Spectral"):
            ramp = style.colorRamp(n)
            if ramp is not None:
                return ramp
        return QgsGradientColorRamp(QColor(0, 0, 255), QColor(255, 0, 0))

    #: Status-line icons by level (info lines carry no icon).
    _STATUS_ICONS: ClassVar[dict[str, str]] = {"ok": "✅", "warn": "⚠️", "error": "❌", "info": ""}

    def _set_status(self, text: str, level: str = "info") -> None:
        """Show a single status line (plain text, auto-escaped) tagged with ``level``'s icon."""
        self._render_status([(level, escape(text))])

    def _render_status(self, lines: list[tuple[str, str]]) -> None:
        """Render ``(level, html)`` lines, one per row, each prefixed by its status icon."""
        rows = [
            f'<div style="margin:2px 0">{self._STATUS_ICONS.get(level, "")} {html}</div>'
            for level, html in lines
        ]
        self._status_html = "".join(rows)  # remembered so the log view can return to it
        self._status.setHtml(self._status_html)

    def _on_status_anchor(self, url: QUrl) -> None:
        """Handle a click in the status view: show a run log inline, or go back to the summary."""
        target = url.toString()
        if target == "almo:back":
            self._status.setHtml(self._status_html)  # restore the study/run summary
        elif target == "almo:editor" and self._current_log:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._current_log))  # open in the OS editor
        elif url.isLocalFile():
            self._show_log(url.toLocalFile())

    def _show_log(self, path: str) -> None:
        """Show a run's log inside the status view, with a link back to the study summary."""
        self._current_log = path
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = f"(could not read log: {exc})"
        if len(text) > 200_000:  # keep a very long log manageable in the panel
            text = "… (truncated to the last 200 KB) …\n" + text[-200_000:]
        name = escape(os.path.basename(path))
        header = (
            '<div style="margin:2px 0">⬅ <a href="almo:back">Back to study log</a> · '
            f'<a href="almo:editor">open in editor</a> — <code>{name}</code></div>'
        )
        self._status.setHtml(f"{header}<pre style='white-space:pre-wrap'>{escape(text)}</pre>")


class _ResampleJob:
    """Pipeline step: resample a raster input in the background, then re-point the input at it."""

    weight = 0.15  # typically much shorter than a simulation

    def __init__(
        self, page: RunPage, name: str, src: str, dst: str, resolution: float, alg: str
    ) -> None:
        self._page = page
        self._name = name
        self._src = src
        self._dst = dst
        self._res = resolution
        self._alg = alg
        self.label = f"Resampling '{name}' to {resolution:g} m"
        self.result: Any = None  # a resample contributes no run result
        self._task: ResampleTask | None = None

    def make_task(self) -> ResampleTask:
        self._task = ResampleTask(self._src, self._dst, self._res, self._alg)
        return self._task

    def finalize(self) -> None:
        task = self._task
        if task is None or task.error:
            raise RuntimeError(task.error if task else "resample did not run")
        if not task.out_path or not os.path.exists(task.out_path):
            raise RuntimeError("the resample produced no output")
        self._page._load_resampled_input(self._name, task.out_path)


class _RunJob:
    """Pipeline step: run one simulation. Its run-spec is built when the step starts, so it sees
    any inputs re-pointed by earlier resample steps.

    ``sample`` is ``None`` for the single deterministic run (parameters/name/folder come from the
    page), or a ``{"run_id", "params", "output_dir", "weight"}`` dict for one sample of a sweep."""

    weight = 1.0

    def __init__(self, page: RunPage, sample: dict[str, Any] | None = None) -> None:
        self._page = page
        self._sample = sample
        self.label = "Running the simulation"
        self.result: Any = None
        self._task: RunTask | None = None

    def make_task(self) -> RunTask:
        if self._sample is None:
            runspec = self._page._build_runspec()  # may raise -> pipeline reports it as failed
        else:
            runspec = self._page._build_runspec(
                run_id=self._sample["run_id"],
                params=self._sample["params"],
                output_dir=self._sample["output_dir"],
            )
        self.label = f"Running '{runspec.run_id}'"
        self._task = RunTask(self._page._state.manifest, runspec)
        return self._task

    def finalize(self) -> None:
        self.result = self._task.result if self._task is not None else None


class _SweepJob:
    """Pipeline step: run a whole sweep through the engine scheduler as one background task.

    The per-sample run-specs are built here (main thread) so they see any inputs re-pointed by
    earlier resample/copy steps, then handed to a :class:`SweepTask` that drives them with the
    chosen concurrency and retries. ``result`` is the list of every run's :class:`RunResult`.
    """

    weight = 1.0

    def __init__(
        self,
        page: RunPage,
        manifest: Any,
        samples: list[dict[str, Any]],
        index: Any,
        *,
        max_workers: int,
        retries: int,
    ) -> None:
        self._page = page
        self._manifest = manifest
        self._samples = samples
        self._index = index
        self._max_workers = max_workers
        self._retries = retries
        self.label = f"Running {len(samples)} simulations"
        self.result: Any = None
        self._task: SweepTask | None = None

    def make_task(self) -> SweepTask:
        sweep_runs = [
            engine.SweepRun(
                runspec=self._page._build_runspec(
                    run_id=s["run_id"], params=s["params"], output_dir=s["output_dir"]
                ),
                weight=s["weight"],
            )
            for s in self._samples
        ]
        self._task = SweepTask(
            self._manifest,
            sweep_runs,
            self._index,
            max_workers=self._max_workers,
            retries=self._retries,
        )
        return self._task

    def finalize(self) -> None:
        self.result = list(self._task.results) if self._task is not None else []


class _CopyInputsTask(QgsTask):
    """Background copy of input datasets into the project's ``inputs/`` folder.

    Each copy op is ``(name, src, dst)``: it copies ``src`` (and its sidecars) to ``dst``, which
    may carry a new basename when a name conflict was resolved by renaming. ``result_paths`` maps
    each input name to the copied primary file, for the runs to read.
    """

    def __init__(self, copies: list[tuple[str, str, str]]) -> None:
        super().__init__("Copy inputs", QgsTask.Flag.CanCancel)
        self._copies = copies
        self.result_paths: dict[str, str] = {}
        self.error: str | None = None

    def run(self) -> bool:
        try:
            total = len(self._copies) or 1
            for i, (name, src, dst) in enumerate(self._copies):
                if self.isCanceled():
                    return False
                self.result_paths[name] = _copy_dataset(src, dst)
                self.setProgress((i + 1) / total * 100.0)
        except Exception as exc:  # a failed copy must fail the run, not silently run stale inputs
            self.error = str(exc)
            return False
        return not self.isCanceled()


class _CopyInputsJob:
    """Pipeline step: copy the run's inputs into ``<project>/inputs/`` and point the runs at them.

    Runs after any resample step, so it copies the resampled rasters actually used. Inputs already
    inside the project (or identical copies already there) are reused in place; a same-name file
    with different data prompts the user (resolved on the main thread in :meth:`make_task`). A
    no-op when nothing needs copying."""

    weight = 0.1  # file copy; typically short next to a simulation

    def __init__(self, page: RunPage, manifest: Any, inputs_dir: str) -> None:
        self._page = page
        self._manifest = manifest
        self._inputs_dir = inputs_dir
        self.label = "Copying inputs into the project"
        self.result: Any = None  # contributes no run result
        self._reuse: dict[str, str] = {}
        self._task: _CopyInputsTask | None = None

    def make_task(self) -> _CopyInputsTask:
        # Resolve (checksum + prompt on conflict) on the main thread; raises if the user cancels.
        copies, self._reuse = self._page._resolve_inputs_copy_plan(self._manifest, self._inputs_dir)
        self._task = _CopyInputsTask(copies)
        return self._task

    def finalize(self) -> None:
        task = self._task
        if task is None or task.error:
            raise RuntimeError(task.error if task else "input copy did not run")
        # The runs read the in-project paths (freshly copied, or an identical file already there).
        self._page._inputs_override.update(self._reuse)
        self._page._inputs_override.update(task.result_paths)


def _is_within(path: str, root: str) -> bool:
    """True if ``path`` is inside directory ``root`` (so it need not be copied again)."""
    root_abs = os.path.abspath(root)
    try:
        return os.path.commonpath([os.path.abspath(path), root_abs]) == root_abs
    except ValueError:  # different drives on Windows -> not within
        return False


def _copy_dataset(src: str, dst: str) -> str:
    """Copy dataset ``src`` to ``dst`` (its primary file), with sidecars, and return ``dst``.

    Sidecars are files whose name is ``<stem>.<something>`` (a shapefile's ``.shx``/``.dbf``/
    ``.prj``/``.cpg``, a raster's ``.prj``/``.tfw``/``.tif.aux.xml``), so the copied dataset is
    complete and openable. When ``dst`` has a different stem from ``src`` (a rename on conflict),
    every sidecar is renamed to match, keeping the dataset consistent."""
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    src_stem, tgt_stem = source.stem, target.stem
    prefix = src_stem + "."
    for sibling in source.parent.iterdir():
        if sibling.is_file() and (sibling.name == source.name or sibling.name.startswith(prefix)):
            suffix = sibling.name[len(src_stem) :]  # ".tif", ".prj", ".tif.aux.xml"
            shutil.copy2(sibling, target.parent / (tgt_stem + suffix))
    return str(target)


def _unique_dst(inputs_dir: str, src: str) -> str:
    """A non-colliding ``inputs_dir`` path from ``src``'s name: ``dem_1.tif``, ``dem_2.tif``, …"""
    source = Path(src)
    stem = source.stem
    suffix = source.name[len(stem) :]
    i = 1
    while True:
        candidate = os.path.join(inputs_dir, f"{stem}_{i}{suffix}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _same_file(a: str, b: str) -> bool:
    """True if two files have identical contents (size check, then SHA-256)."""
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    return _sha256(a) == _sha256(b)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _layer_local_path(layer: Any) -> str | None:
    """The existing absolute file backing ``layer``, or None if it is not saved locally.

    Only plain GDAL/OGR file sources count. Memory/temporary layers, remote providers (WMS/WFS/
    vsicurl, …) and relative/bare paths (e.g. a project-embedded ``dem.tif`` with no directory)
    all return None, so the caller knows to save them to a real file before running.
    """
    with contextlib.suppress(Exception):
        if layer.isTemporary():
            return None
    if layer.providerType() not in ("gdal", "ogr"):
        return None
    # OGR appends "|layername=…"/"|layerid=…"; the file is the part before the first '|'.
    path = layer.source().split("|", 1)[0].strip()
    if os.path.isabs(path) and os.path.exists(path):
        return path
    return None


def _save_raster(layer: QgsRasterLayer, path: str) -> str:
    """Write ``layer`` to a GeoTIFF at ``path`` and return the path."""
    provider = layer.dataProvider()
    writer = QgsRasterFileWriter(path)
    writer.setOutputFormat("GTiff")
    pipe = QgsRasterPipe()
    if not pipe.set(provider.clone()):
        raise RuntimeError("could not build the raster pipe")
    err = writer.writeRaster(
        pipe, provider.xSize(), provider.ySize(), provider.extent(), layer.crs()
    )
    if err != QgsRasterFileWriter.WriterError.NoError:
        raise RuntimeError(f"raster writer error ({err})")
    return path


def _save_vector(layer: QgsVectorLayer, path: str) -> str:
    """Write ``layer`` to ``path`` (format from extension) and return the written path."""
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "ESRI Shapefile" if path.lower().endswith(".shp") else "GPKG"
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), options
    )
    # writeAsVectorFormatV3 -> (errorCode, errorMessage, newFilename?, newLayer?)
    if result[0] != QgsVectorFileWriter.WriterError.NoError:
        raise RuntimeError(result[1] or f"vector writer error ({result[0]})")
    return path
