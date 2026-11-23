"""The Results page: inspect a finished (or running) sweep and build its hazard maps.

A sweep can be thousands of runs, so this page never loads every run's temporal output. Instead it:

* reads the project's ``runs.json`` / ``study.json`` into a summary (scheme, swept parameters,
  per-run outcome and weight) and shows it as a header plus a per-run table;
* on demand, reduces every successful run's ``max_values`` raster -- weighted by each sample's
  recorded weight -- into a few hazard maps (impact probability, weighted mean depth/velocity,
  worst-case envelope, optional weighted percentile) and loads just those, filling the table's
  per-run metric columns from the same pass.

The heavy raster work runs off the main thread in :class:`_AggregateTask`.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from qgis.core import (
    QgsApplication,
    QgsColorRampShader,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsStyle,
    QgsTask,
)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis.results.aggregate import (
    AggregateConfig,
    AggregateError,
    AggregateInput,
    AggregateResult,
    aggregate_max_values,
)
from almo_sweep_qgis.results.load_run import load_run_outputs
from almo_sweep_qgis.results.study import StudySummary, load_study
from almo_sweep_qgis.state import StudyState

#: Friendly display name for each aggregate layer key (see results.aggregate.LAYER_FILENAMES).
_LAYER_LABELS = {
    "impact_probability": "Impact probability",
    "mean_max_height": "Weighted mean max depth",
    "mean_max_velocity": "Weighted mean max velocity",
    "max_height_envelope": "Max depth envelope",
    "percentile_max_height": "Weighted percentile max depth",
}
#: Base (non-metric) table columns; swept-parameter columns are inserted between Weight and these.
_BASE_HEAD = ("Run", "Status", "Weight")
_METRIC_HEAD = (
    "Max depth (m)",
    "Max velocity (m/s)",
    "Impacted area (m²)",
    "Duration (s)",
    "Error",
)


class ResultsPage(QWidget):
    """Browse a sweep's runs and build weighted hazard maps from them."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._summary: StudySummary | None = None
        self._task: _AggregateTask | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # Project picker.
        picker = QHBoxLayout()
        picker.addWidget(QLabel("Project folder"))
        self._folder = QgsFileWidget()
        self._folder.setStorageMode(QgsFileWidget.StorageMode.GetDirectory)
        self._folder.setDialogTitle("Choose a sweep's project folder (contains runs.json)")
        picker.addWidget(self._folder, 1)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self._on_load)
        picker.addWidget(load_btn, 0)
        root.addLayout(picker)

        self._header = QLabel("Load a sweep's project folder to inspect its runs.")
        self._header.setWordWrap(True)
        self._header.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self._header)

        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Click a column header to sort by it (click again to flip ascending/descending). Numeric
        # columns sort by value, not text, via _SortableItem (see below).
        self._table.setSortingEnabled(True)
        root.addWidget(self._table, 1)

        row_actions = QHBoxLayout()
        self._load_btn = QPushButton("Load full result")
        self._load_btn.setToolTip(
            "Load the selected run's complete outputs (max values and time series) onto the map, "
            "just like a single run."
        )
        self._load_btn.clicked.connect(self._on_load_run)
        self._load_btn.setEnabled(False)
        row_actions.addWidget(self._load_btn)
        self._open_btn = QPushButton("Open run folder")
        self._open_btn.setToolTip("Open the selected run's folder in the file manager")
        self._open_btn.clicked.connect(self._on_open_folder)
        self._open_btn.setEnabled(False)
        row_actions.addWidget(self._open_btn)
        row_actions.addStretch(1)
        root.addLayout(row_actions)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)

        root.addWidget(self._build_hazard_group())

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        # When a run/sweep finishes, point this page at the project that was just simulated.
        state.projectFinished.connect(self._on_project_finished)

    # -- hazard-map controls -----------------------------------------------------------
    def _build_hazard_group(self) -> QGroupBox:
        group = QGroupBox("Hazard maps")
        box = QVBoxLayout(group)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Impact depth ≥"))
        self._threshold = QDoubleSpinBox()
        self._threshold.setRange(0.0, 1e6)
        self._threshold.setDecimals(3)
        self._threshold.setValue(0.1)
        self._threshold.setSuffix(" m")
        self._threshold.setToolTip("A cell counts as reached when a run's max depth exceeds this.")
        controls.addWidget(self._threshold)
        self._pct_check = QCheckBox("Percentile")
        self._pct_check.setChecked(True)
        controls.addWidget(self._pct_check)
        self._pct = QSpinBox()
        self._pct.setRange(1, 99)
        self._pct.setValue(90)
        self._pct.setSuffix(" %")
        self._pct_check.toggled.connect(self._pct.setEnabled)
        controls.addWidget(self._pct)
        controls.addStretch(1)
        box.addLayout(controls)

        buttons = QHBoxLayout()
        self._compute_btn = QPushButton("Compute hazard maps")
        self._compute_btn.setToolTip(
            "Reduce every successful run's max_values into weighted hazard maps and load them."
        )
        self._compute_btn.clicked.connect(self._on_compute)
        self._compute_btn.setEnabled(False)
        buttons.addWidget(self._compute_btn)
        buttons.addStretch(1)
        box.addLayout(buttons)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        box.addWidget(self._progress)
        return group

    def _on_project_finished(self, project_root: str) -> None:
        """Auto-fill the folder with the just-simulated project; load it if it has a run index."""
        self._folder.setFilePath(project_root)
        if os.path.exists(os.path.join(project_root, "runs.json")):
            self._on_load()

    # -- loading a study ---------------------------------------------------------------
    def _on_load(self) -> None:
        root = self._folder.filePath().strip()
        if not root or not os.path.isdir(root):
            self._set_status("Choose a project folder first.")
            return
        self._summary = load_study(root, self._state.manifest)
        self._populate_header()
        self._populate_table()
        self._compute_btn.setEnabled(self._summary.n_success > 0)
        if self._summary.n_total == 0:
            self._set_status("No runs found here yet (no runs.json).")
        else:
            self._set_status(f"Loaded {self._summary.n_total} run(s).")

    def _populate_header(self) -> None:
        s = self._summary
        if s is None:
            return
        lines = [
            f"<b>{s.project}</b> — {s.manifest.get('name', 'model')} "
            f"{s.manifest.get('version', '')}",
            f"Scheme: <b>{s.scheme or 'n/a'}</b>"
            + (f", seed {s.seed}" if s.seed is not None else "")
            + f" · Runs: {s.n_total} (✓ {s.n_success} / ✗ {s.n_failed})",
        ]
        if s.swept:
            swept = "; ".join(f"{p.header} ~ {p.summary()}" for p in s.swept)
            lines.append(f"Swept: {swept}")
        if s.total_duration_s:
            lines.append(f"Total compute time: {s.total_duration_s:.0f} s")
        dirty = s.dirty_components()
        if dirty:
            lines.append(
                f"⚠ Not reproducible — git-dirty at run time: {', '.join(sorted(set(dirty)))}"
            )
        fails = s.failures()
        if fails:
            lines.append("Failures: " + "; ".join(f"{n}x {msg}" for msg, n in fails.items()))
        self._header.setText("<br>".join(lines))

    def _populate_table(self) -> None:
        s = self._summary
        if s is None:
            return
        swept_cols = [p.name for p in s.swept]
        swept_labels = [p.header for p in s.swept]
        headers = [*_BASE_HEAD, *swept_labels, *_METRIC_HEAD]
        # Disable sorting while filling: with it on, each setItem would re-sort and shuffle the rows
        # we are still writing. Re-enabled at the end; the table starts in run order until clicked.
        self._table.setSortingEnabled(False)
        self._table.setColumnCount(len(headers))
        self._table.setHorizontalHeaderLabels(headers)
        self._table.setRowCount(len(s.runs))
        for row, run in enumerate(s.runs):
            dur = "" if run.duration_s is None else f"{run.duration_s:.1f}"
            cells = [
                _text_cell(run.run_id),
                _text_cell("✓" if run.ok else "✗"),
                _num_cell(f"{run.weight:.4g}", run.weight),
                *[_param_cell(run.params.get(c)) for c in swept_cols],
                _num_cell("", None),  # Max depth (filled after aggregation)
                _num_cell("", None),  # Max velocity
                _num_cell("", None),  # Impacted area
                _num_cell(dur, run.duration_s),
                _text_cell(run.error or ""),
            ]
            for col, item in enumerate(cells):
                self._table.setItem(row, col, item)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._metric_base = len(_BASE_HEAD) + len(swept_cols)  # first metric column
        self._table.setSortingEnabled(True)

    # -- aggregation -------------------------------------------------------------------
    def _on_compute(self) -> None:
        s = self._summary
        if s is None:
            return
        inputs: list[AggregateInput] = []
        missing = 0
        for run in s.successful_runs():
            path = run.max_values_path()
            if path and os.path.exists(path):
                inputs.append(AggregateInput(run.run_id, path, run.weight))
            else:
                missing += 1
        if not inputs:
            self._set_status("No successful run has a max_values raster to aggregate.")
            return
        config = AggregateConfig(
            threshold=self._threshold.value(),
            percentile=float(self._pct.value()) if self._pct_check.isChecked() else None,
        )
        out_dir = os.path.join(s.project_root, "aggregates")
        self._compute_btn.setEnabled(False)
        self._progress.setValue(0)
        note = f" ({missing} run(s) skipped — no raster)" if missing else ""
        self._set_status(f"Aggregating {len(inputs)} run(s){note}…")
        self._task = _AggregateTask(inputs, out_dir, config)
        self._task.taskCompleted.connect(self._on_aggregate_done)
        self._task.taskTerminated.connect(self._on_aggregate_failed)
        self._task.progressChanged.connect(lambda pct: self._progress.setValue(int(pct)))
        QgsApplication.taskManager().addTask(self._task)

    def _on_aggregate_done(self) -> None:
        task, self._task = self._task, None
        self._compute_btn.setEnabled(True)
        self._progress.setValue(100)
        if task is None or task.result is None:
            return
        self._fill_metrics(task.result)
        loaded = self._load_aggregate_layers(task.result.layers)
        self._set_status(
            f"Hazard maps from {task.result.n_runs} run(s): {loaded} layer(s) added under "
            f"'Hazard maps'. Per-run metrics filled in the table."
        )

    def _on_aggregate_failed(self) -> None:
        task, self._task = self._task, None
        self._compute_btn.setEnabled(True)
        msg = (task.error if task else None) or "aggregation failed"
        self._set_status(f"Could not build hazard maps: {msg}")

    def _fill_metrics(self, result: AggregateResult) -> None:
        base = getattr(self, "_metric_base", len(_BASE_HEAD))
        # Sorting may have moved rows since the table was built, so resolve each run's row by its id
        # (column 0) rather than by a stored index. Pause sorting so the writes don't reshuffle.
        self._table.setSortingEnabled(False)
        for run_id, m in result.per_run.items():
            row = self._row_for_run(run_id)
            if row is None:
                continue
            self._table.setItem(row, base, _num_cell(f"{m.max_height:.3g}", m.max_height))
            self._table.setItem(row, base + 1, _num_cell(f"{m.max_velocity:.3g}", m.max_velocity))
            self._table.setItem(row, base + 2, _num_cell(f"{m.impacted_area:.0f}", m.impacted_area))
        self._table.setSortingEnabled(True)

    def _row_for_run(self, run_id: str) -> int | None:
        """The current visual row whose Run cell (column 0) holds ``run_id``, or None."""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.text() == run_id:
                return row
        return None

    def _load_aggregate_layers(self, layers: dict[str, str]) -> int:
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        name = f"Hazard maps — {self._summary.project if self._summary else 'sweep'}"
        group = root.insertGroup(0, name)
        order = [
            "impact_probability",
            "mean_max_height",
            "percentile_max_height",
            "mean_max_velocity",
            "max_height_envelope",
        ]
        loaded = 0
        for key in order:
            path = layers.get(key)
            if not path or not os.path.exists(path):
                continue
            layer = QgsRasterLayer(path, _LAYER_LABELS.get(key, key))
            if not layer.isValid():
                continue
            self._style(layer, key)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            loaded += 1
        return loaded

    #: Colour (and visibility) floor: cells below this are treated as "not reached" and, with the
    #: shader's clip-out-of-range on, render transparent — so dry background never masks the hazard.
    _MIN_VISIBLE = 0.001

    def _style(self, layer: QgsRasterLayer, key: str) -> None:
        """Single-band pseudocolour from 0.001 to the data max, clipping out-of-range cells away."""
        provider = layer.dataProvider()
        lo, hi = self._MIN_VISIBLE, 1.0
        if key != "impact_probability":
            with contextlib.suppress(Exception):
                stats = provider.bandStatistics(1)
                hi = float(stats.maximumValue)
        if hi <= lo:
            hi = lo + 1.0
        ramp = _ramp("Reds" if key == "impact_probability" else "Viridis")
        shader = QgsRasterShader()
        color_ramp = QgsColorRampShader(lo, hi)
        color_ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
        color_ramp.setClip(True)  # values outside [lo, hi] (e.g. dry 0 cells) render transparent
        if ramp is not None:
            color_ramp.setSourceColorRamp(ramp)
            color_ramp.classifyColorRamp(5, 1)
        shader.setRasterShaderFunction(color_ramp)
        renderer = QgsSingleBandPseudoColorRenderer(provider, 1, shader)
        layer.setRenderer(renderer)

    # -- per-run actions ---------------------------------------------------------------
    def _selected_run(self) -> Any | None:
        if self._summary is None:
            return None
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return None
        run_id_item = self._table.item(rows[0].row(), 0)
        if run_id_item is None:
            return None
        run_id = run_id_item.text()
        return next((r for r in self._summary.runs if r.run_id == run_id), None)

    def _on_selection_changed(self) -> None:
        run = self._selected_run()
        self._open_btn.setEnabled(run is not None)
        # Only a successful run has outputs to load.
        self._load_btn.setEnabled(run is not None and run.ok)

    def _on_load_run(self) -> None:
        """Load the selected sweep run's complete outputs, exactly as a single run is loaded."""
        run = self._selected_run()
        if run is None:
            return
        outputs = run.outputs()
        if not outputs:
            self._set_status(
                f"No outputs recorded for {run.run_id} (result.json missing or empty)."
            )
            return
        outcome = load_run_outputs(
            outputs,
            run.run_id,
            manifest=self._state.manifest,
            output_dt=self._run_output_dt(run),
        )
        if outcome.layers == 0:
            self._set_status(f"Could not load any layers for {run.run_id}.")
            return
        hint = (
            " Open View ▸ Panels ▸ Temporal Controller (animation mode) to step through the frames."
            if outcome.frame_series
            else ""
        )
        self._set_status(
            f"Loaded {run.run_id}: {outcome.layers} layer(s) added under '{run.run_id}'.{hint}"
        )

    def _run_output_dt(self, run: Any) -> float:
        """Temporal frame spacing (s) for a run: its recorded value, else the manifest default."""
        for source in (run.params, self._state.param_values):
            value = source.get("output_dt")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                return float(value)
        manifest = self._state.manifest
        spec = getattr(manifest, "params", {}).get("output_dt") if manifest is not None else None
        default = getattr(spec, "default", None)
        if isinstance(default, (int, float)) and not isinstance(default, bool) and default:
            return float(default)
        return 1.0

    def _on_open_folder(self) -> None:
        run = self._selected_run()
        if run is None:
            return
        path = Path(run.output_dir)
        if not path.exists():
            self._set_status(f"Folder not found: {path}")
            return
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _set_status(self, text: str) -> None:
        self._status.setText(text)


class _AggregateTask(QgsTask):
    """Runs the raster aggregation off the main thread."""

    def __init__(self, inputs: list[AggregateInput], out_dir: str, config: AggregateConfig) -> None:
        super().__init__("Aggregate hazard maps", QgsTask.Flag.CanCancel)
        self._inputs = inputs
        self._out_dir = out_dir
        self._config = config
        self.result: AggregateResult | None = None
        self.error: str | None = None

    def run(self) -> bool:
        try:
            self.result = aggregate_max_values(
                self._inputs,
                self._out_dir,
                self._config,
                progress=lambda frac, _msg: self.setProgress(frac * 100.0),
                cancel=self.isCanceled,
            )
        except AggregateError as exc:
            self.error = str(exc)
            return False
        except Exception as exc:  # unexpected (GDAL, I/O) — report rather than crash the task
            self.error = str(exc)
            return False
        return not self.isCanceled()


def _ramp(name: str) -> Any | None:
    with contextlib.suppress(Exception):
        return QgsStyle.defaultStyle().colorRamp(name)
    return None


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return "" if value is None else str(value)


#: Role on a table item holding its numeric sort key (None for text-only cells).
_SORT_ROLE = Qt.ItemDataRole.UserRole


class _SortableItem(QTableWidgetItem):
    """A table cell that sorts by its numeric key when it has one, else by displayed text.

    Qt's default ``QTableWidgetItem`` compares display strings, so "100" would sort before "9".
    Numeric columns (weight, swept values, metrics, duration) store their value under
    :data:`_SORT_ROLE` and compare on it; a blank numeric cell (no key) sorts after any value.
    """

    def __lt__(self, other: QTableWidgetItem) -> bool:
        a = self.data(_SORT_ROLE)
        b = other.data(_SORT_ROLE)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a < b
        if isinstance(a, (int, float)) != isinstance(b, (int, float)):
            # Keep rows with a value ahead of blank ones regardless of sort direction's base order.
            return isinstance(a, (int, float))
        return super().__lt__(other)


def _text_cell(text: str) -> _SortableItem:
    return _SortableItem(text)


def _num_cell(text: str, value: float | int | None) -> _SortableItem:
    item = _SortableItem(text)
    if isinstance(value, (int, float)):
        item.setData(_SORT_ROLE, float(value))
    return item


def _param_cell(value: Any) -> _SortableItem:
    """A swept-parameter cell: numeric-sortable when the value is a number, else plain text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _num_cell(_fmt(value), float(value))
    return _text_cell(_fmt(value))
