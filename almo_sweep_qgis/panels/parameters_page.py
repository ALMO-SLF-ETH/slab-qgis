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

from pathlib import Path
from typing import Any

from qgis.core import Qgis, QgsMapLayer, QgsProject, QgsRasterLayer, QgsVectorLayer
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
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

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
                field = self._make_input_control(spec.name, spec)
                self._add_row(form, box, _input_label(spec), field, spec)
            self._groups_layout.addWidget(box)

        self._apply_visibility()

    def _add_row(
        self, form: QFormLayout, box: QGroupBox, label: QWidget, field: QWidget, spec: Any
    ) -> None:
        index = form.rowCount()
        form.addRow(label, field)
        self._rows.append(_Row(spec, form, index, box, label, field))

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

    # -- input controls ----------------------------------------------------------------
    def _make_input_control(self, name: str, spec: Any) -> QWidget:
        """A layer picker plus a '…' button to load a file from disk and select it.

        Picking a file loads it as a project layer and selects it in the combo, so it behaves
        exactly like an already-loaded layer from then on (and the run reads its source).
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
        return container

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
