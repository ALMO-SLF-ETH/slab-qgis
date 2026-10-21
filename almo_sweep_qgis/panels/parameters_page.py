"""The Parameters page: the model's inputs *and* parameters, built from its manifest.

Everything the user configures about a run lives here, laid out under the manifest's group
headings in declaration order. Within each group the scalar parameters are rendered first,
then the spatial inputs (a DEM raster picker, a release-zone layer picker, ...), so a layer
can sit beside the parameter that selects it.

Each control is matched to its spec: a double spin box (float) or spin box (int) honouring
bounds and unit, a combo (choice), or a checkbox (bool) for params; a geometry-filtered
``QgsMapLayerComboBox`` for inputs. Parameter edits go to ``StudyState.param_values`` and
input selections to ``StudyState.input_layers``; the Run page reads both.

Both params and inputs honour ``visible_when``: a control whose conditions (evaluated against
the current *parameter* values) are not met is hidden, and re-shown the moment they are -- so,
e.g., the release-zone layer appears only for a POLYGON release and the rectangle fields only
for a RECTANGLE release. Visibility is cosmetic: hidden params keep their value and are still
sent to the model.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayer,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import estimate
from almo_sweep_qgis.dem.resample import (
    RESAMPLE_METHODS,
    ResampleError,
    default_resampled_path,
    raster_resolution,
    resample_raster,
)
from almo_sweep_qgis.dem.style import apply_hillshade
from almo_sweep_qgis.state import StudyState

# Fallback bounds when the manifest leaves a numeric parameter unbounded.
_FLOAT_LIMIT = 1.0e12
_INT_LIMIT = 2_147_483_647  # QSpinBox is limited to 32-bit ints


class _Row:
    """One param or input's place in the form, so visibility can be toggled later.

    ``spec`` is either a ParamSpec or an InputSpec; both expose ``is_visible(param_values)``.
    """

    __slots__ = ("box", "field", "form", "index", "label", "spec")

    def __init__(
        self,
        spec: Any,
        form: QFormLayout,
        index: int,
        box: QGroupBox,
        label: QWidget,
        field: QWidget,
    ) -> None:
        self.spec = spec
        self.form = form
        self.index = index
        self.box = box
        self.label = label
        self.field = field


class ParametersPage(QWidget):
    """Renders the model's inputs and parameters, grouped, and captures edits into state."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._rows: list[_Row] = []
        self._input_combos: dict[str, QgsMapLayerComboBox] = {}
        self._wired_polygon: QgsVectorLayer | None = None  # release layer we listen to for edits

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self._empty = QLabel("Select a valid model manifest above to configure the run.")
        self._empty.setWordWrap(True)
        self._empty.setEnabled(False)
        root.addWidget(self._empty)

        self._groups_host = QWidget(self)
        self._groups_layout = QVBoxLayout(self._groups_host)
        self._groups_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._groups_host)
        root.addStretch(1)

        # Live release-size estimate labels, (re)created inside the release group during _rebuild;
        # None for a model that has no release section.
        self._area_label: QLabel | None = None
        self._volume_label: QLabel | None = None

        state.manifestChanged.connect(self._rebuild)
        self._rebuild(state.manifest)

    def _rebuild(self, manifest: Any | None) -> None:
        while self._groups_layout.count():
            item = self._groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        self._input_combos.clear()
        self._area_label = None  # recreated below only if there is a release section
        self._volume_label = None

        has_content = manifest is not None and (manifest.params or manifest.inputs)
        self._empty.setVisible(not has_content)
        self._groups_host.setVisible(bool(has_content))
        if not has_content:
            return

        for group, params, inputs in manifest.grouped_items():
            if not params and not inputs:
                continue
            box = QGroupBox(group.display_label or "Parameters")
            if group.description:
                box.setToolTip(group.description)
            form = QFormLayout(box)
            for spec in params:  # parameters first, then the inputs that relate to them
                field = self._make_param_control(spec.name, spec)
                self._add_row(form, box, _param_label(spec), field, spec)
                self._state.param_values[spec.name] = spec.default
            for spec in inputs:
                field, combo = self._make_input_control(spec.name, spec)
                self._input_combos[spec.name] = combo
                self._add_row(form, box, _input_label(spec), field, spec)
                if spec.kind == "raster":
                    # Below a raster input: show its resolution and offer to resample it. Tied to
                    # the input's own spec so it shows/hides exactly when the input does.
                    panel = _ResamplePanel(combo)
                    self._add_full_row(form, box, panel, spec)
            # The release section (the group that selects the release shape) gets a live
            # area/volume estimate right below its selection controls.
            if any(p.name == "release_type" for p in params):
                self._add_estimate_rows(form)
            self._groups_layout.addWidget(box)

        self._apply_visibility()
        self._refresh_estimate()

    def _add_row(
        self, form: QFormLayout, box: QGroupBox, label: QWidget, field: QWidget, spec: Any
    ) -> None:
        index = form.rowCount()
        form.addRow(label, field)
        self._rows.append(_Row(spec, form, index, box, label, field))

    def _add_full_row(self, form: QFormLayout, box: QGroupBox, widget: QWidget, spec: Any) -> None:
        """Add a widget that spans the whole form row (no separate label), e.g. a sub-panel."""
        index = form.rowCount()
        form.addRow(widget)
        self._rows.append(_Row(spec, form, index, box, widget, widget))

    # -- visibility --------------------------------------------------------------------
    def _apply_visibility(self) -> None:
        """Show/hide each row (and empty group boxes) per the params' current values."""
        current = self._state.param_values
        box_has_visible: dict[int, bool] = {}
        boxes: dict[int, QGroupBox] = {}
        for row in self._rows:
            visible = row.spec.is_visible(current)
            self._set_row_visible(row, visible)
            key = id(row.box)
            boxes[key] = row.box
            box_has_visible[key] = box_has_visible.get(key, False) or visible
        for key, box in boxes.items():
            box.setVisible(box_has_visible[key])

    @staticmethod
    def _set_row_visible(row: _Row, visible: bool) -> None:
        setter = getattr(row.form, "setRowVisible", None)
        if callable(setter):
            setter(row.index, visible)
        else:  # pragma: no cover - Qt builds without setRowVisible
            row.label.setVisible(visible)
            row.field.setVisible(visible)

    # -- parameter controls ------------------------------------------------------------
    def _make_param_control(self, name: str, spec: Any) -> QWidget:
        ptype = str(spec.type)
        if ptype == "float":
            return _with_unit(self._float_control(name, spec), spec.unit)
        if ptype == "int":
            return _with_unit(self._int_control(name, spec), spec.unit)
        if ptype == "bool":
            return self._bool_control(name, spec)
        if ptype == "choice":
            return self._choice_control(name, spec)
        label = QLabel(str(spec.default))
        label.setEnabled(False)
        return label

    def _float_control(self, name: str, spec: Any) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setDecimals(6)
        box.setRange(
            spec.min if spec.min is not None else -_FLOAT_LIMIT,
            spec.max if spec.max is not None else _FLOAT_LIMIT,
        )
        box.setToolTip(spec.description or "")
        box.setValue(float(spec.default))
        box.valueChanged.connect(lambda value, n=name: self._set_param(n, float(value)))
        return box

    def _int_control(self, name: str, spec: Any) -> QSpinBox:
        box = QSpinBox()
        box.setRange(
            int(spec.min) if spec.min is not None else -_INT_LIMIT,
            int(spec.max) if spec.max is not None else _INT_LIMIT,
        )
        box.setToolTip(spec.description or "")
        box.setValue(int(spec.default))
        box.valueChanged.connect(lambda value, n=name: self._set_param(n, int(value)))
        return box

    def _bool_control(self, name: str, spec: Any) -> QCheckBox:
        box = QCheckBox()
        box.setToolTip(spec.description or "")
        box.setChecked(bool(spec.default))
        box.toggled.connect(lambda checked, n=name: self._set_param(n, bool(checked)))
        return box

    def _choice_control(self, name: str, spec: Any) -> QComboBox:
        box = QComboBox()
        choices = list(spec.choices)
        box.addItems([str(c) for c in choices])
        box.setToolTip(spec.description or "")
        if spec.default in choices:
            box.setCurrentIndex(choices.index(spec.default))
        box.currentIndexChanged.connect(
            lambda index, n=name, c=choices: self._set_param(n, c[index])
        )
        return box

    def _set_param(self, name: str, value: Any) -> None:
        self._state.param_values[name] = value
        # A change may reveal or hide dependent params/inputs (visible_when).
        self._apply_visibility()
        # ...and may change the release size (shape, depth, geometry).
        self._refresh_estimate()

    # -- input controls ----------------------------------------------------------------
    def _make_input_control(self, name: str, spec: Any) -> tuple[QWidget, QgsMapLayerComboBox]:
        """A layer picker plus a '…' button to load a file from disk and select it.

        Picking a file loads it as a project layer and selects it in the combo, so it behaves
        exactly like an already-loaded layer from then on (and the run reads its source). Returns
        the row widget and the combo (so a raster input's resample panel can track it).
        """
        combo = QgsMapLayerComboBox()
        combo.setFilters(_filter_for(spec))
        if not spec.required:
            combo.setAllowEmptyLayer(True)
        combo.setToolTip(spec.description or "")
        combo.layerChanged.connect(lambda layer, n=name: self._set_input(n, layer))

        browse = QToolButton()
        browse.setText("…")
        browse.setToolTip("Load a file from disk")
        browse.clicked.connect(lambda _=False, s=spec, c=combo: self._browse_input_file(s, c))

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(combo, 1)
        layout.addWidget(browse, 0)
        self._set_input(name, combo.currentLayer())
        return container, combo

    def _browse_input_file(self, spec: Any, combo: QgsMapLayerComboBox) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {spec.display_label}", "", _file_filter(spec)
        )
        if not path:
            return
        name = Path(path).stem
        if spec.kind == "raster":
            layer: QgsRasterLayer | QgsVectorLayer = QgsRasterLayer(path, name)
        else:
            layer = QgsVectorLayer(path, name, "ogr")
        if not layer.isValid():
            combo.setToolTip(f"Could not load: {path}")
            return
        QgsProject.instance().addMapLayer(layer)
        combo.setLayer(layer)  # fires layerChanged -> _set_input

    def _set_input(self, name: str, layer: QgsMapLayer | None) -> None:
        self._state.input_layers[name] = layer.id() if layer is not None else None
        if name == "release_zones":
            # Re-estimate automatically when the user commits edits to the release polygon layer.
            self._listen_for_edits(layer)
        self._refresh_estimate()

    def _listen_for_edits(self, layer: QgsMapLayer | None) -> None:
        """(Re)connect the selected release-zone layer's commit signals to the estimate refresh."""
        if self._wired_polygon is not None:
            for signal in (
                self._wired_polygon.afterCommitChanges,
                self._wired_polygon.editingStopped,
            ):
                with contextlib.suppress(Exception):
                    signal.disconnect(self._refresh_estimate)
            self._wired_polygon = None
        if isinstance(layer, QgsVectorLayer):
            # afterCommitChanges: edits saved while staying in edit mode; editingStopped: toggling
            # edit mode off (which commits/rolls back). Either way the geometry on disk may differ.
            with contextlib.suppress(Exception):
                layer.afterCommitChanges.connect(self._refresh_estimate)
            with contextlib.suppress(Exception):
                layer.editingStopped.connect(self._refresh_estimate)
            self._wired_polygon = layer

    # -- release estimate --------------------------------------------------------------
    def _add_estimate_rows(self, form: QFormLayout) -> None:
        """Add the live area/volume estimate under the release section's selection controls."""
        heading = QLabel("<b>Release estimate</b> <i>(approx.)</i>")
        form.addRow(heading)
        self._area_label = QLabel("—")
        self._volume_label = QLabel("—")
        form.addRow("Area", self._area_label)
        form.addRow("Volume", self._volume_label)

    def _refresh_estimate(self) -> None:
        """Update the area/volume labels for the current release configuration."""
        if self._area_label is None or self._volume_label is None:
            return  # model has no release section
        est = estimate.compute_release_estimate(
            self._state.param_values,
            polygon_area_m2=self._polygon_area(),
            release_raster_path=self._release_raster_path(),
        )
        self._area_label.setText(_fmt_area(est.area_m2))
        self._volume_label.setText(_fmt_volume(est.volume_m3))
        tip = est.detail or ""
        self._area_label.setToolTip(tip)
        self._volume_label.setToolTip(tip)

    def _polygon_area(self) -> float | None:
        """Total planar area (CRS units²) of the selected release-zone polygon layer, if any."""
        layer = self._layer_for("release_zones")
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            return None
        total = 0.0
        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom is not None and not geom.isEmpty():
                total += geom.area()
        return total

    def _release_raster_path(self) -> str | None:
        """Local file path of the selected release raster, if it is on disk."""
        layer = self._layer_for("release_raster")
        if layer is None:
            return None
        src = layer.source().split("|", 1)[0].strip()
        return src if os.path.isabs(src) and os.path.exists(src) else None

    def _layer_for(self, input_name: str) -> QgsMapLayer | None:
        layer_id = self._state.input_layers.get(input_name)
        if not layer_id:
            return None
        return QgsProject.instance().mapLayer(layer_id)


def _param_label(spec: Any) -> QLabel:
    label = QLabel(f"<b>{spec.display_label}</b>")
    label.setToolTip(spec.description or "")
    return label


def _input_label(spec: Any) -> QLabel:
    # A conditional input (visible_when) is required whenever it is shown, so it is not marked
    # "(optional)"; only a truly always-optional input is.
    truly_optional = not spec.required and not spec.visible_when
    suffix = " <i>(optional)</i>" if truly_optional else ""
    label = QLabel(f"<b>{spec.display_label}</b>{suffix}")
    label.setToolTip(spec.description or "")
    return label


def _file_filter(spec: Any) -> str:
    if spec.kind == "raster":
        return "Raster (*.asc *.tif *.tiff);;All files (*)"
    geometry = (spec.geometry or "").lower()
    if geometry == "polygon":
        return "Vector (*.shp *.gpkg *.geojson);;All files (*)"
    return "Vector (*.shp *.gpkg *.geojson *.kml);;All files (*)"


def _filter_for(spec: Any) -> Qgis.LayerFilter:
    """Map an input spec to a QGIS layer-combo filter."""
    if spec.kind == "raster":
        return Qgis.LayerFilter.RasterLayer
    geometry = (spec.geometry or "").lower()
    by_geometry = {
        "polygon": getattr(Qgis.LayerFilter, "PolygonLayer", Qgis.LayerFilter.VectorLayer),
        "line": getattr(Qgis.LayerFilter, "LineLayer", Qgis.LayerFilter.VectorLayer),
        "point": getattr(Qgis.LayerFilter, "PointLayer", Qgis.LayerFilter.VectorLayer),
    }
    return by_geometry.get(geometry, Qgis.LayerFilter.VectorLayer)


def _with_unit(control: QWidget, unit: str | None) -> QWidget:
    """Place the unit as a label to the right of the field, so the field holds only a value."""
    if not unit:
        return control
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(control, 1)
    layout.addWidget(QLabel(unit))
    return container


def _fmt_area(area: float | None) -> str:
    return "—" if area is None else f"{area:,.1f} m²"


def _fmt_volume(volume: float | None) -> str:
    if volume is None:
        return "—"
    if abs(volume) >= 1.0e6:  # also give an easier-to-read Mm³ for large releases
        return f"{volume:,.0f} m³  ({volume / 1.0e6:,.2f} Mm³)"
    return f"{volume:,.1f} m³"


def _local_raster_path(layer: QgsRasterLayer | None) -> str | None:
    """The absolute file backing a raster layer, or None if it is not a plain file on disk."""
    if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
        return None
    src = layer.source().split("|", 1)[0].strip()
    return src if os.path.isabs(src) and os.path.exists(src) else None


class _ResamplePanel(QWidget):
    """Resolution display + optional resampling for a raster input (sits under its picker).

    Shows the selected raster's native resolution, and -- when enabled -- resamples it to a chosen
    resolution and method, writing a new GeoTIFF (defaulting to the source name + ``_<res>m``) and
    selecting it in the input's combo so the run uses it. No-op if the target equals the native
    resolution.
    """

    def __init__(self, combo: QgsMapLayerComboBox, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._combo = combo
        self._task: _ResampleTask | None = None
        self._out_edited = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 0, 0, 6)  # indent under the picker
        outer.setSpacing(2)

        self._res_label = QLabel("Resolution: —")
        self._res_label.setEnabled(False)
        outer.addWidget(self._res_label)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        self._enable = QCheckBox("Resample to")
        self._target = QDoubleSpinBox()
        self._target.setDecimals(3)
        self._target.setRange(0.001, 100000.0)
        self._target.setValue(2.0)
        self._target.setSuffix(" m")
        self._method = QComboBox()
        for label, alg in RESAMPLE_METHODS:
            self._method.addItem(label, alg)
        controls.addWidget(self._enable)
        controls.addWidget(self._target)
        controls.addWidget(self._method, 1)
        outer.addLayout(controls)

        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        self._out = QLineEdit()
        self._out.setPlaceholderText("Output raster path (defaults beside the source)")
        out_browse = QToolButton()
        out_browse.setText("…")
        out_browse.setToolTip("Choose where to save the resampled raster")
        self._apply = QPushButton("Apply")
        out_row.addWidget(QLabel("Save as"))
        out_row.addWidget(self._out, 1)
        out_row.addWidget(out_browse)
        out_row.addWidget(self._apply)
        outer.addLayout(out_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setEnabled(False)
        outer.addWidget(self._status)

        self._combo.layerChanged.connect(lambda _layer: self._on_layer_changed())
        self._enable.toggled.connect(self._on_enabled)
        self._target.valueChanged.connect(lambda _v: self._refresh_default_out())
        self._out.textEdited.connect(self._on_out_edited)
        out_browse.clicked.connect(self._browse_out)
        self._apply.clicked.connect(self._on_apply)

        self._on_enabled(False)
        self._on_layer_changed()

    # -- reactions ---------------------------------------------------------------------
    def _on_layer_changed(self) -> None:
        native = self._native_res()
        if native is None:
            self._res_label.setText("Resolution: — (no raster, or not saved to disk)")
        else:
            x, y = native
            same = abs(x - y) < 1e-9
            self._res_label.setText(
                f"Resolution: {x:g} m" if same else f"Resolution: {x:g} x {y:g} m"
            )
        self._out_edited = False  # a new source resets the default output name
        self._refresh_default_out()
        self._update_apply_enabled()

    def _on_enabled(self, on: bool) -> None:
        for widget in (self._target, self._method, self._out, self._apply):
            widget.setEnabled(on)
        self._update_apply_enabled()

    def _on_out_edited(self, _text: str) -> None:
        self._out_edited = True

    def _refresh_default_out(self) -> None:
        if self._out_edited:
            return
        src = _local_raster_path(self._combo.currentLayer())
        if src is not None:
            self._out.setText(default_resampled_path(src, self._target.value()))

    def _update_apply_enabled(self) -> None:
        enabled = self._enable.isChecked() and _local_raster_path(self._combo.currentLayer())
        self._apply.setEnabled(bool(enabled))

    def _browse_out(self) -> None:
        start = self._out.text().strip() or ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save resampled raster", start, "GeoTIFF (*.tif)"
        )
        if path:
            self._out.setText(path)
            self._out_edited = True

    # -- apply -------------------------------------------------------------------------
    def _on_apply(self) -> None:
        src = _local_raster_path(self._combo.currentLayer())
        if src is None:
            self._set_status("Select a raster saved on disk first.")
            return
        res = float(self._target.value())
        native = self._native_res()
        if native is not None and abs(native[0] - res) < 1e-9 and abs(native[1] - res) < 1e-9:
            self._set_status(f"Already at {res:g} m — no resampling needed.")
            return
        dst = self._out.text().strip() or default_resampled_path(src, res)
        alg = self._method.currentData()

        self._apply.setEnabled(False)
        self._set_status(f"Resampling to {res:g} m…")
        task = _ResampleTask(src, dst, res, alg)
        task.taskCompleted.connect(lambda t=task: self._on_done(t))
        task.taskTerminated.connect(lambda t=task: self._on_done(t))
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _on_done(self, task: _ResampleTask) -> None:
        self._task = None
        self._update_apply_enabled()
        if task.error is not None:
            self._set_status(f"Resample failed: {task.error}")
            return
        if not task.out_path or not os.path.exists(task.out_path):
            self._set_status("Resample cancelled.")
            return
        layer = QgsRasterLayer(task.out_path, Path(task.out_path).stem)
        if not layer.isValid():
            self._set_status(f"Resampled file could not be loaded: {task.out_path}")
            return
        QgsProject.instance().addMapLayer(layer)
        apply_hillshade(layer)
        self._combo.setLayer(layer)  # fires layerChanged -> becomes the input; updates the label
        self._set_status(f"Resampled to {task.out_path}")

    # -- helpers -----------------------------------------------------------------------
    def _native_res(self) -> tuple[float, float] | None:
        layer = self._combo.currentLayer()
        if isinstance(layer, QgsRasterLayer) and layer.isValid():
            return abs(layer.rasterUnitsPerPixelX()), abs(layer.rasterUnitsPerPixelY())
        src = _local_raster_path(layer if isinstance(layer, QgsRasterLayer) else None)
        return raster_resolution(src) if src else None

    def _set_status(self, text: str) -> None:
        self._status.setText(text)


class _ResampleTask(QgsTask):
    """Background raster resample (GDAL warp), so the UI stays responsive on large DEMs."""

    def __init__(self, src: str, dst: str, resolution: float, alg: str) -> None:
        super().__init__("Resample raster", QgsTask.Flag.CanCancel)
        self._src = src
        self._dst = dst
        self._res = resolution
        self._alg = alg
        self.out_path: str | None = None
        self.error: str | None = None

    def run(self) -> bool:
        try:
            self.out_path = resample_raster(
                self._src,
                self._dst,
                self._res,
                self._alg,
                progress=lambda frac, _msg: self.setProgress(frac * 100.0),
                should_cancel=self.isCanceled,
            )
        except ResampleError as exc:
            self.error = str(exc)
            return False
        except Exception as exc:  # defensive: surface any unexpected GDAL error
            self.error = str(exc)
            return False
        return not self.isCanceled()
