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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayer,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, pyqtSignal
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
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis import estimate, sweep_plan
from almo_sweep_qgis.dem.resample import (
    RESAMPLE_METHODS,
    default_resampled_path,
    raster_resolution,
)
from almo_sweep_qgis.dem.style import apply_hillshade
from almo_sweep_qgis.panels.run_pipeline import ResampleTask
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
        self._numeric_fields: dict[str, _NumericParamField] = {}  # sweepable numeric params
        self._wired_polygon: QgsVectorLayer | None = None  # release layer we listen to for edits
        self._building = False  # suppress sweep sync while (re)building the form

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self._empty = QLabel("Select a valid model manifest above to configure the run.")
        self._empty.setWordWrap(True)
        self._empty.setEnabled(False)
        root.addWidget(self._empty)

        # Parameter-sweep toggle: turning it on reveals a "sweep" box on each (currently visible)
        # sweepable numeric parameter and the sweep-method controls below, so everything about the
        # study lives on this one page.
        self._sweep_toggle = QCheckBox("Enable parameter sweep (choose which parameters to vary)")
        self._sweep_toggle.setObjectName("sweepEnable")
        self._sweep_toggle.setToolTip("Vary selected parameters across many simulations.")
        self._sweep_toggle.toggled.connect(self._on_sweep_toggle)
        root.addWidget(self._sweep_toggle)

        # The sweep *method* (scheme + steps/samples/seed) and the live run count: shown only while
        # the sweep is enabled. Kept outside the scroll area so it stays visible.
        self._method_box = self._build_method_box()
        root.addWidget(self._method_box)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setVisible(False)
        root.addWidget(self._summary)

        # The parameters/inputs list can be long and vertical space is limited, so it scrolls.
        self._groups_host = QWidget()
        self._groups_layout = QVBoxLayout(self._groups_host)
        self._groups_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setWidget(self._groups_host)
        root.addWidget(self._scroll, 1)

        # Live release-size estimate labels, (re)created inside the release group during _rebuild;
        # None for a model that has no release section.
        self._area_label: QLabel | None = None
        self._volume_label: QLabel | None = None

        # When a selected layer is deleted, fall back to "no layer" (before QGIS destroys it, so we
        # never touch a dangling layer) -- the user must then re-pick a valid one.
        with contextlib.suppress(Exception):
            QgsProject.instance().layersWillBeRemoved.connect(self._on_layers_will_be_removed)

        state.manifestChanged.connect(self._rebuild)
        # When another page swaps an input's layer (e.g. the Run page resamples at launch), reflect
        # it in the matching combo so the UI stays in sync.
        state.inputLayerReplaced.connect(self._on_input_layer_replaced)
        self._rebuild(state.manifest)

    def _on_input_layer_replaced(self, name: str, layer_id: Any) -> None:
        combo = self._input_combos.get(name)
        if combo is None:
            return
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if layer is not None and combo.currentLayer() is not layer:
            combo.setLayer(layer)  # fires layerChanged -> _set_input (same id, harmless)

    def _on_layers_will_be_removed(self, layer_ids: Any) -> None:
        """Reset any input whose selected layer is about to be removed back to 'no layer'."""
        removed = set(layer_ids)
        for combo in self._input_combos.values():
            layer = combo.currentLayer()
            if layer is not None and layer.id() in removed:
                combo.setLayer(None)  # fires layerChanged -> clears state and edit listeners

    # -- sweep method (scheme + steps/samples/seed) ------------------------------------
    def _build_method_box(self) -> QGroupBox:
        box = QGroupBox("Sweep method")
        box.setObjectName("sweepMethodBox")
        outer = QVBoxLayout(box)

        scheme_row = QHBoxLayout()
        scheme_row.addWidget(QLabel("Scheme"))
        self._scheme_combo = QComboBox()
        for key, label in sweep_plan.SCHEMES:
            self._scheme_combo.addItem(label, key)
        self._scheme_combo.setToolTip(
            "Grid: every combination of equal steps (deterministic). "
            "Latin hypercube: stratified, space-filling. Monte Carlo: random draws."
        )
        self._scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        scheme_row.addWidget(self._scheme_combo, 1)
        outer.addLayout(scheme_row)

        self._scheme_stack = QStackedWidget()
        self._scheme_stack.addWidget(self._build_grid_controls())  # 0: grid
        self._scheme_stack.addWidget(self._build_sampler_controls())  # 1: LHS / Monte Carlo
        outer.addWidget(self._scheme_stack)
        return box

    def _build_grid_controls(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        self._steps_spin = QSpinBox()
        self._steps_spin.setRange(1, 1000)
        self._steps_spin.setValue(sweep_plan._DEFAULT_STEPS)
        self._steps_spin.setToolTip("Default number of grid points per parameter.")
        self._steps_spin.valueChanged.connect(self._on_method_changed)
        form.addRow("Steps per parameter", self._steps_spin)
        self._spacing_combo = QComboBox()
        self._spacing_combo.addItem("Linear", "linear")
        self._spacing_combo.addItem("Logarithmic", "log")
        self._spacing_combo.setToolTip("Logarithmic spacing needs a strictly positive range.")
        self._spacing_combo.currentIndexChanged.connect(self._on_method_changed)
        form.addRow("Spacing", self._spacing_combo)
        return page

    def _build_sampler_controls(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        self._samples_spin = QSpinBox()
        self._samples_spin.setRange(1, 1_000_000)
        self._samples_spin.setValue(50)
        self._samples_spin.setToolTip("Number of simulations to draw.")
        self._samples_spin.valueChanged.connect(self._on_method_changed)
        form.addRow("Samples", self._samples_spin)

        seed_row = QHBoxLayout()
        self._seed_mode = QComboBox()
        self._seed_mode.addItem("Random each run", False)
        self._seed_mode.addItem("Fixed seed", True)
        self._seed_mode.setToolTip("A fixed seed makes the draw reproducible across runs.")
        self._seed_mode.currentIndexChanged.connect(self._on_seed_mode_changed)
        self._seed_spin = QSpinBox()
        self._seed_spin.setRange(0, 2_147_483_647)
        self._seed_spin.setValue(0)
        self._seed_spin.setEnabled(False)
        self._seed_spin.valueChanged.connect(self._on_method_changed)
        seed_row.addWidget(self._seed_mode)
        seed_row.addWidget(self._seed_spin, 1)
        form.addRow("Seed", seed_row)
        return page

    def _on_scheme_changed(self, _index: int = 0) -> None:
        scheme = self._scheme_combo.currentData() or "grid"
        self._scheme_stack.setCurrentIndex(0 if scheme == "grid" else 1)
        self._sync_sweep()

    def _on_seed_mode_changed(self, _index: int) -> None:
        self._seed_spin.setEnabled(bool(self._seed_mode.currentData()))
        self._sync_sweep()

    def _on_method_changed(self, *_args: Any) -> None:
        self._sync_sweep()

    def _rebuild(self, manifest: Any | None) -> None:
        self._building = True
        while self._groups_layout.count():
            item = self._groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()
        self._input_combos.clear()
        self._numeric_fields.clear()
        self._area_label = None  # recreated below only if there is a release section
        self._volume_label = None

        has_content = manifest is not None and (manifest.params or manifest.inputs)
        self._empty.setVisible(not has_content)
        self._scroll.setVisible(bool(has_content))
        # A fresh model starts with the sweep off; the enable toggle only shows with content, and
        # the method box + summary stay hidden until the sweep is enabled.
        self._sweep_toggle.blockSignals(True)
        self._sweep_toggle.setChecked(False)
        self._sweep_toggle.blockSignals(False)
        self._sweep_toggle.setVisible(bool(has_content))
        self._scheme_combo.setCurrentIndex(0)
        self._method_box.setVisible(False)
        self._summary.setVisible(False)
        if not has_content:
            self._building = False
            return

        for group, params, inputs in manifest.grouped_items():
            if not params and not inputs:
                continue
            box = QGroupBox(group.display_label or "Parameters")
            if group.description:
                box.setToolTip(group.description)
            form = QFormLayout(box)
            for spec in params:  # parameters first, then the inputs that relate to them
                self._add_param_control(form, box, spec)
                self._state.param_values[spec.name] = spec.default
            for spec in inputs:
                field, combo = self._make_input_control(spec.name, spec)
                self._input_combos[spec.name] = combo
                self._add_row(form, box, _input_label(spec), field, spec)
                if spec.kind == "raster":
                    # Below a raster input: show its resolution and offer to resample it. Tied to
                    # the input's own spec so it shows/hides exactly when the input does.
                    panel = _ResamplePanel(combo, self._state, spec.name)
                    self._add_full_row(form, box, panel, spec)
            # The release section (the group that selects the release shape) gets a live
            # area/volume estimate right below its selection controls.
            if any(p.name == "release_type" for p in params):
                self._add_estimate_rows(form)
            self._groups_layout.addWidget(box)

        self._apply_visibility()
        self._refresh_estimate()
        self._building = False
        self._refresh_scheme_mode()  # align each sweep editor with the current scheme
        self._sync_sweep()  # publish the (initially empty, disabled) sweep config

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
    def _add_param_control(self, form: QFormLayout, box: QGroupBox, spec: Any) -> None:
        """Add a parameter's row. A sweepable numeric parameter becomes a full-width
        :class:`_NumericParamField` (sweep checkbox on the left, value on the right, inline range
        editor, tinted when swept); every other parameter is a plain label + control row."""
        ptype = str(spec.type)
        if ptype in ("float", "int") and getattr(spec, "sweepable", False):
            spin = (
                self._float_control(spec.name, spec)
                if ptype == "float"
                else (self._int_control(spec.name, spec))
            )
            field = _NumericParamField(spec, _param_label(spec), _with_unit(spin, spec.unit), spin)
            field.changed.connect(self._sync_sweep)
            # A swept release parameter (e.g. depth) feeds the live estimate via its mean/bounds, so
            # keep area/volume in step with the distribution editor too, not only fixed-value edits.
            field.changed.connect(self._refresh_estimate)
            self._numeric_fields[spec.name] = field
            self._add_full_row(form, box, field, spec)
        else:
            control = self._make_param_control(spec.name, spec)
            self._add_row(form, box, _param_label(spec), control, spec)

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
        # A newly-hidden parameter must drop out of the sweep; a newly-shown one may join it.
        self._sync_sweep()

    # -- parameter sweep ---------------------------------------------------------------
    def _on_sweep_toggle(self, on: bool) -> None:
        self._method_box.setVisible(on)
        self._summary.setVisible(on)
        for field in self._numeric_fields.values():
            field.set_sweep_mode(on)
        self._sync_sweep()
        # Turning the sweep off reverts the estimate to fixed values; on, a swept depth may already
        # be ticked, so recompute either way.
        self._refresh_estimate()

    def _sync_sweep(self) -> None:
        """Publish the whole sweep config (enable, per-parameter selection, and method) to state.

        Only *currently visible* swept parameters are included, so a parameter hidden by a
        ``visible_when`` (e.g. a friction coefficient for the wrong law) never enters the sweep."""
        if self._building:
            return
        enabled = self._sweep_toggle.isChecked()
        current = self._state.param_values
        params: dict[str, Any] = {}
        if enabled:
            for name, field in self._numeric_fields.items():
                if field.is_swept() and field.spec.is_visible(current):
                    params[name] = field.sweep_entry()
        seed = int(self._seed_spin.value()) if self._seed_mode.currentData() else None
        self._state.sweep = {
            "enabled": enabled,
            "scheme": self._scheme_combo.currentData() or "grid",
            "n_samples": int(self._samples_spin.value()),
            "steps": int(self._steps_spin.value()),
            "spacing": self._spacing_combo.currentData() or "linear",
            "seed": seed,
            "params": params,
        }
        self._refresh_scheme_mode()
        self._refresh_summary()
        self._state.sweepChanged.emit()

    def _refresh_scheme_mode(self) -> None:
        """Show each sweep editor's per-parameter 'Steps' field only for the grid scheme."""
        grid = self._state.sweep.get("scheme", "grid") == "grid"
        for field in self._numeric_fields.values():
            field.set_grid_mode(grid)

    def _refresh_summary(self) -> None:
        sweep = self._state.sweep
        text = sweep_plan.summary_text(sweep)
        count = sweep_plan.projected_count(sweep)
        if sweep.get("enabled") and count > sweep_plan.LARGE_SWEEP:
            self._summary.setText(
                f"⚠ {text} Each is a full model run, so this may take a very long time."
            )
            self._summary.setStyleSheet("color: #b36b00;")
        else:
            self._summary.setText(text)
            self._summary.setStyleSheet("")

    # -- input controls ----------------------------------------------------------------
    def _make_input_control(self, name: str, spec: Any) -> tuple[QWidget, QgsMapLayerComboBox]:
        """A layer picker plus a '…' button to load a file from disk and select it.

        Picking a file loads it as a project layer and selects it in the combo, so it behaves
        exactly like an already-loaded layer from then on (and the run reads its source). Returns
        the row widget and the combo (so a raster input's resample panel can track it).
        """
        combo = QgsMapLayerComboBox()
        combo.setFilters(_filter_for(spec))
        # Always offer "no layer" and start on it, so the user picks the layer deliberately rather
        # than silently running with whatever QGIS happened to auto-select.
        combo.setAllowEmptyLayer(True)
        combo.setToolTip(spec.description or "")
        combo.layerChanged.connect(lambda layer, n=name: self._set_input(n, layer))
        combo.setLayer(None)

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
        """(Re)connect the selected release-zone layer's edit signals to the estimate refresh.

        Both live edit-buffer changes (geometry moved, feature added/deleted while editing) and
        commit/edit-end are wired, so the estimate tracks the polygons as they are drawn, not only
        once saved.
        """
        if self._wired_polygon is not None:
            # The previous layer may already be deleted (C++ object gone) -- e.g. it was removed
            # from the project. Accessing its signals then raises, so guard the whole disconnect.
            with contextlib.suppress(Exception):
                for signal in self._edit_signals(self._wired_polygon):
                    with contextlib.suppress(Exception):
                        signal.disconnect(self._estimate_dirty)
            self._wired_polygon = None
        if isinstance(layer, QgsVectorLayer):
            for signal in self._edit_signals(layer):
                with contextlib.suppress(Exception):
                    signal.connect(self._estimate_dirty)
            self._wired_polygon = layer

    @staticmethod
    def _edit_signals(layer: QgsVectorLayer) -> tuple:
        # Live buffer edits (while in edit mode) + save/end-of-edit, so the estimate updates as the
        # polygons change and again when committed.
        return (
            layer.geometryChanged,
            layer.featureAdded,
            layer.featuresDeleted,
            layer.afterCommitChanges,
            layer.editingStopped,
        )

    def _estimate_dirty(self, *_args: Any) -> None:
        """Signal sink that ignores a signal's arguments and refreshes the estimate."""
        self._refresh_estimate()

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
        """Update the area/volume labels for the current release configuration.

        When parameters are swept (probabilistic analysis) the estimate follows each swept
        parameter's distribution: a Normal parameter contributes its *mean* (so the estimate is a
        single central figure), while a Uniform parameter contributes its *low* and *high* bounds,
        yielding a low-to-high range for area and volume. Fixed parameters use their set value.
        """
        if self._area_label is None or self._volume_label is None:
            return  # model has no release section
        low_params, high_params, has_range = self._estimate_param_variants()
        est_low = estimate.compute_release_estimate(
            low_params,
            polygon_area_m2=self._polygon_area(),
            release_raster_path=self._release_raster_path(),
        )
        if not has_range:
            # Every relevant parameter resolves to a single value (fixed, or Normal→mean).
            self._area_label.setText(_fmt_area(est_low.area_m2))
            self._volume_label.setText(_fmt_volume(est_low.volume_m3))
            tip = est_low.detail or ""
        else:
            est_high = estimate.compute_release_estimate(
                high_params,
                polygon_area_m2=self._polygon_area(),
                release_raster_path=self._release_raster_path(),
            )
            self._area_label.setText(_fmt_range(est_low.area_m2, est_high.area_m2, _fmt_area))
            self._volume_label.setText(
                _fmt_range(est_low.volume_m3, est_high.volume_m3, _fmt_volume)
            )
            note = "low-to-high range over Uniform-swept parameters"
            base = est_low.detail or ""
            tip = f"{base}; {note}" if base else note
        self._area_label.setToolTip(tip)
        self._volume_label.setToolTip(tip)

    def _estimate_param_variants(self) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Build the "low" and "high" parameter dicts the estimate is evaluated at.

        Both start from the current fixed values. For each *visible, swept* numeric parameter a
        Normal distribution pins both dicts to its mean; a Uniform distribution sets the low dict
        to its ``low`` bound and the high dict to its ``high`` bound. Returns the two dicts plus a
        flag that is true when at least one Uniform-swept parameter makes the two differ.
        """
        current = self._state.param_values
        low = dict(current)
        high = dict(current)
        has_range = False
        for name, field in self._numeric_fields.items():
            if not (field.is_swept() and field.spec.is_visible(current)):
                continue
            entry = field.sweep_entry()
            if entry.get("dist") == "normal":
                mean = entry.get("mean")
                low[name] = mean
                high[name] = mean
            else:  # uniform
                low[name] = entry.get("low")
                high[name] = entry.get("high")
                has_range = True
        return low, high, has_range

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


def _fmt_range(
    a: float | None, b: float | None, fmt: Callable[[float | None], str]
) -> str:
    """Format two endpoint estimates as a 'low to high' range using ``fmt``.

    The bounds are ordered (``min``/``max``) because a parameter need not be monotonic increasing
    in the estimate: a larger minimum release depth, for instance, releases *fewer* raster cells.
    When both endpoints coincide (or either is missing) it collapses to a single formatted value.
    """
    if a is None or b is None:
        return fmt(a if b is None else b)
    lo, hi = (a, b) if a <= b else (b, a)
    if lo == hi:
        return fmt(lo)
    return f"{fmt(lo)} to {fmt(hi)}"


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

    def __init__(
        self,
        combo: QgsMapLayerComboBox,
        state: StudyState,
        input_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._combo = combo
        self._state = state
        self._input_name = input_name
        self._task: ResampleTask | None = None
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
        self._target.valueChanged.connect(self._on_target_changed)
        self._method.currentIndexChanged.connect(lambda _i: self._publish_request())
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
        self._publish_request()

    def _on_enabled(self, on: bool) -> None:
        for widget in (self._target, self._method, self._out, self._apply):
            widget.setEnabled(on)
        self._update_apply_enabled()
        self._publish_request()

    def _on_target_changed(self, _value: float) -> None:
        self._refresh_default_out()
        self._publish_request()

    def _on_out_edited(self, _text: str) -> None:
        self._out_edited = True
        self._publish_request()

    def _refresh_default_out(self) -> None:
        if self._out_edited:
            return
        src = _local_raster_path(self._combo.currentLayer())
        if src is not None:
            self._out.setText(default_resampled_path(src, self._target.value()))

    def _publish_request(self) -> None:
        """Mirror the armed resample into shared state, so the Run page can apply it at launch."""
        if self._enable.isChecked():
            self._state.resample_requests[self._input_name] = {
                "resolution": float(self._target.value()),
                "alg": self._method.currentData(),
                "out_path": self._out.text().strip(),
            }
        else:
            self._state.resample_requests.pop(self._input_name, None)

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
        task = ResampleTask(src, dst, res, alg)
        task.taskCompleted.connect(lambda t=task: self._on_done(t))
        task.taskTerminated.connect(lambda t=task: self._on_done(t))
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _on_done(self, task: ResampleTask) -> None:
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


class _SweepEditor(QWidget):
    """Range + distribution editor for one swept numeric parameter (Uniform or Normal).

    Emits :attr:`changed` on any edit. :meth:`entry` returns the plain-data config consumed by
    :mod:`~almo_sweep_qgis.sweep_plan` (``{"dist": "uniform", "low", "high"}`` or
    ``{"dist": "normal", "mean", "std", "min", "max"}``, plus a grid ``steps`` override)."""

    changed = pyqtSignal()

    def __init__(self, spec: Any, base_value: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._grid = True
        lo, hi = _numeric_bounds(spec)

        form = QFormLayout(self)
        form.setContentsMargins(18, 0, 0, 0)  # indent under the value + "Sweep" checkbox

        self._dist = QComboBox()
        self._dist.addItem("Uniform", "uniform")
        self._dist.addItem("Normal", "normal")
        self._dist.setCurrentIndex(self._dist.findData("normal"))  # Normal is the default profile
        self._dist.setToolTip("Weight profile: Uniform (equal) or Normal (Gaussian).")
        self._dist.currentIndexChanged.connect(self._on_dist)
        form.addRow("Distribution", self._dist)

        self._low = _editor_spin(lo, hi, lo)
        self._high = _editor_spin(lo, hi, hi)
        self._mean = _editor_spin(lo, hi, base_value)
        span = (hi - lo) if (hi - lo) > 0 else (abs(base_value) or 1.0)
        self._std = _editor_spin(0.0, _FLOAT_LIMIT, round(span / 6.0, 6))
        for spin in (self._low, self._high, self._mean, self._std):
            spin.valueChanged.connect(lambda _v: self.changed.emit())

        self._uniform_row = _pair("Low", self._low, "High", self._high)
        self._normal_row = _pair("Mean", self._mean, "Std", self._std)
        form.addRow(self._uniform_row)
        form.addRow(self._normal_row)

        self._steps = QSpinBox()
        self._steps.setRange(0, 1000)
        self._steps.setValue(0)
        self._steps.setSpecialValueText("(default)")  # 0 -> use the sweep's default steps
        self._steps.setToolTip("Grid points for this parameter (0 uses the sweep default).")
        self._steps.valueChanged.connect(lambda _v: self.changed.emit())
        self._steps_label = QLabel("Steps")
        form.addRow(self._steps_label, self._steps)

        self._apply_dist_visibility()

    def set_grid_mode(self, on: bool) -> None:
        """Show the per-parameter 'Steps' field only for the grid scheme."""
        self._grid = on
        self._steps.setVisible(on)
        self._steps_label.setVisible(on)

    def entry(self) -> dict[str, Any]:
        if self._dist.currentData() == "normal":
            data: dict[str, Any] = {
                "dist": "normal",
                "mean": float(self._mean.value()),
                "std": float(self._std.value()),
                "min": _opt(self._spec.min),
                "max": _opt(self._spec.max),
            }
        else:
            data = {
                "dist": "uniform",
                "low": float(self._low.value()),
                "high": float(self._high.value()),
            }
        if self._grid and self._steps.value() > 0:
            data["steps"] = int(self._steps.value())
        return data

    def _on_dist(self, _index: int) -> None:
        self._apply_dist_visibility()
        self.changed.emit()

    def _apply_dist_visibility(self) -> None:
        is_normal = self._dist.currentData() == "normal"
        self._uniform_row.setVisible(not is_normal)
        self._normal_row.setVisible(is_normal)


class _NumericParamField(QWidget):
    """A full-width row for a sweepable numeric parameter: a 'sweep' checkbox on the **left**, then
    the label and value control, then an inline range/distribution editor shown when swept.

    Off (sweep toggle off), the checkbox is hidden and it reads as a normal labelled value. When
    the toggle is on, the checkbox appears; ticking it reveals the editor and tints the row's
    background so swept parameters stand out. ``spec`` is kept so the page can test the parameter's
    ``visible_when`` before including it in the sweep."""

    changed = pyqtSignal()

    def __init__(
        self,
        spec: Any,
        label: QWidget,
        control: QWidget,
        spin: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._sweep_mode = False
        self.setObjectName("numericParamField")
        # A plain QWidget ignores a stylesheet background unless it paints a styled background.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        box = QVBoxLayout(self)
        box.setContentsMargins(4, 2, 4, 2)
        box.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self._check = QCheckBox()
        self._check.setObjectName("sweepSelect")
        self._check.setToolTip("Vary this parameter across the sweep instead of a fixed value.")
        self._check.setVisible(False)
        self._check.toggled.connect(self._on_toggle)
        top.addWidget(self._check, 0)
        top.addWidget(label, 0)
        self._control = control
        top.addWidget(control, 1)
        box.addLayout(top)

        self._editor = _SweepEditor(spec, float(spin.value()))
        self._editor.setVisible(False)
        self._editor.changed.connect(self.changed)
        box.addWidget(self._editor)

    def set_sweep_mode(self, on: bool) -> None:
        self._sweep_mode = on
        self._check.setVisible(on)
        self._editor.setVisible(on and self._check.isChecked())
        # When swept, the range/distribution editor replaces the fixed value, so hide the control.
        self._control.setVisible(not self.is_swept())
        self._apply_tint()

    def set_grid_mode(self, on: bool) -> None:
        self._editor.set_grid_mode(on)

    def is_swept(self) -> bool:
        return self._sweep_mode and self._check.isChecked()

    def sweep_entry(self) -> dict[str, Any]:
        return self._editor.entry()

    def _on_toggle(self, on: bool) -> None:
        self._editor.setVisible(self._sweep_mode and on)
        self._control.setVisible(not self.is_swept())
        self._apply_tint()
        self.changed.emit()

    def _apply_tint(self) -> None:
        # A translucent amber highlight with a left accent bar when this parameter is swept, so it
        # is easy to spot and works in both light and dark themes. Scoped to this widget's own id so
        # it does not recolour the child spin boxes.
        if self.is_swept():
            self.setStyleSheet(
                "QWidget#numericParamField { background-color: rgba(240, 180, 40, 0.32); "
                "border-left: 3px solid rgba(210, 140, 10, 0.95); border-radius: 3px; }"
            )
        else:
            self.setStyleSheet("")


def _numeric_bounds(spec: Any) -> tuple[float, float]:
    lo = float(spec.min) if spec.min is not None else -_FLOAT_LIMIT
    hi = float(spec.max) if spec.max is not None else _FLOAT_LIMIT
    return lo, hi


def _editor_spin(lo: float, hi: float, value: float) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setDecimals(6)
    spin.setRange(lo, hi)
    spin.setValue(max(lo, min(hi, value)))
    return spin


def _pair(a_label: str, a: QWidget, b_label: str, b: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(QLabel(a_label))
    layout.addWidget(a, 1)
    layout.addWidget(QLabel(b_label))
    layout.addWidget(b, 1)
    return container


def _opt(value: Any) -> float | None:
    return None if value is None else float(value)
