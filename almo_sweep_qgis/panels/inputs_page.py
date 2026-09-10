"""The Inputs page: one native layer-picker per input the manifest declares.

The page is entirely manifest-driven -- it holds no knowledge of any particular model. When
the active manifest changes it rebuilds its form: a raster picker for each ``raster`` input
and a geometry-filtered vector picker for each ``vector`` input. Selections are written back
to the shared :class:`~almo_sweep_qgis.state.StudyState`.
"""

from __future__ import annotations

from typing import Any

from qgis.core import Qgis, QgsMapLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import QFormLayout, QLabel, QVBoxLayout, QWidget

from almo_sweep_qgis.state import StudyState


class InputsPage(QWidget):
    """Renders and captures the spatial inputs required by the active model."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._combos: dict[str, QgsMapLayerComboBox] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self._empty = QLabel("Select a valid model manifest above to configure its inputs.")
        self._empty.setWordWrap(True)
        self._empty.setEnabled(False)
        root.addWidget(self._empty)

        self._form_host = QWidget(self)
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._form_host)
        root.addStretch(1)

        state.manifestChanged.connect(self._rebuild)
        self._rebuild(state.manifest)

    def _rebuild(self, manifest: Any | None) -> None:
        while self._form.count():
            item = self._form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Reparent out synchronously so the widget leaves the tree at once;
                # deleteLater() only frees it later, once an event loop runs.
                widget.setParent(None)
                widget.deleteLater()
        self._combos.clear()

        has_inputs = manifest is not None and len(manifest.inputs) > 0
        self._empty.setVisible(not has_inputs)
        self._form_host.setVisible(has_inputs)
        if not has_inputs:
            return

        for name, spec in manifest.inputs.items():
            combo = QgsMapLayerComboBox()
            combo.setFilters(_filter_for(spec))
            if not spec.required:
                combo.setAllowEmptyLayer(True)
            combo.setToolTip(spec.description or "")
            combo.layerChanged.connect(lambda layer, n=name: self._on_layer_changed(n, layer))
            self._form.addRow(_label_for(spec), combo)
            self._combos[name] = combo
            self._on_layer_changed(name, combo.currentLayer())

    def _on_layer_changed(self, name: str, layer: QgsMapLayer | None) -> None:
        self._state.input_layers[name] = layer.id() if layer is not None else None


def _label_for(spec: Any) -> QLabel:
    suffix = "" if spec.required else " <i>(optional)</i>"
    label = QLabel(f"<b>{spec.name}</b>{suffix}")
    label.setToolTip(spec.description or "")
    return label


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
