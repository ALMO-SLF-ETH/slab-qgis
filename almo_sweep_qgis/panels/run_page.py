"""The Run page: name the run, choose an output folder, launch, watch progress, load results.

This closes the deterministic single-run workflow. Parameters use the model's manifest
defaults (parameter editing arrives on the Parameters page); the run executes on a background
:class:`~almo_sweep_qgis.panels.run_task.RunTask` and its output layers are added to the
project when it finishes.
"""

from __future__ import annotations

import contextlib
import glob
import os
from typing import Any

from qgis.core import (
    QgsApplication,
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
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import QDate, QDateTime, QTime
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFileDialog,
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
        self._loaded_frame_series = False

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
            being_edited = self._inputs_being_edited(manifest)
            if being_edited:
                # The run reads each layer's file on disk, not its in-memory edit buffer, so an
                # input left in edit mode (or with unsaved edits) would simulate geometry that no
                # longer matches the screen. Block until the user commits and leaves edit mode.
                self._set_status(
                    "Finish editing and save the layer(s) before running — they have open edits "
                    "that are not on disk yet, so the run would use the old geometry: "
                    + ", ".join(being_edited)
                )
                return
            # The model reads each input from a file path, so any input that is only a temporary /
            # in-memory / project-embedded layer (no file on disk) must be saved first.
            if not self._ensure_inputs_saved(manifest):
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
            self._set_status(f"Could not save '{label}': {exc}")
            return False
        # Re-point the layer at the new file so the run (and future runs) read it from disk.
        provider = "gdal" if is_raster else "ogr"
        layer.setDataSource(saved, layer.name(), provider, QgsDataProvider.ProviderOptions())
        return True

    def _suggested_save_path(self, layer: Any, is_raster: bool) -> str:
        base = self._out_dir.filePath().strip() or os.path.expanduser("~")
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in layer.name()) or "layer"
        return os.path.join(base, name + (".tif" if is_raster else ".gpkg"))

    def _build_runspec(self) -> Any:
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
                inputs[name] = layer.source()
                # Capture the layer's CRS so the model can georeference its outputs. An ESRI ASCII
                # DEM carries no CRS inline, so this is the only reliable source. Prefer the compact
                # authority id (e.g. "EPSG:2056"); fall back to WKT for a custom CRS.
                crs = layer.crs()
                if crs is not None and crs.isValid():
                    input_crs[name] = crs.authid() or crs.toWkt()

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
            input_crs=input_crs,
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
            msg = f"Run '{result.run_id}' complete — loaded {loaded} output layer(s)."
            if self._loaded_frame_series:
                msg += (
                    " Open View ▸ Panels ▸ Temporal Controller (animation mode) "
                    "to step through the frames."
                )
            self._set_status(msg)
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
        self._loaded_frame_series = False
        for name, path in result.outputs.items():
            # A directory output holding a raster-frame series (e.g. dampm's grid_series) is
            # loaded as a temporal group so it can be stepped through in time.
            if os.path.isdir(path):
                loaded += self._load_frame_series(
                    name, path, result.run_id, result.outputs.get("max_values")
                )
                continue
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

    #: Frame-raster bands to present as temporal groups: (band index, subgroup label). Matches
    #: dampm's grid rasters (band 1 = flow height h, band 2 = 2D speed v).
    _FRAME_BANDS = ((1, "flow height h"), (2, "velocity v_2D"))

    def _load_frame_series(
        self, name: str, folder: str, run_id: str, max_values_path: str | None = None
    ) -> int:
        """Load a folder of raster frames as temporal groups for the Temporal Controller.

        One subgroup per band in :attr:`_FRAME_BANDS`, each frame a timestamp (spaced by the
        model's output interval). Frames are rendered single-band pseudocolor with the **Turbo**
        ramp; the colour upper bound per band is taken from ``max_values.tif`` (the per-cell
        maxima over the whole run), so the colours are consistent across every frame *and* equal
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

        parent = project.layerTreeRoot().insertGroup(0, f"{name} — {run_id}")
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
                layer.dataProvider().setNoDataValue(band, -9999.0)  # dampm's nodata sentinel

                renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), band)
                renderer.setClassificationMin(0.0)
                if upper is not None and upper > 0.0:
                    renderer.setClassificationMax(float(upper))
                renderer.createShader(ramp.clone())
                layer.setRenderer(renderer)

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

    def _set_status(self, text: str) -> None:
        self._status.setText(text)


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
