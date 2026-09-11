"""The Parameters page: one control per parameter the manifest declares.

Like the Inputs page, this is entirely manifest-driven. Each parameter becomes a control
matched to its type -- a double spin box (float) or spin box (int) honouring the manifest's
bounds and unit, a combo (choice), or a checkbox (bool) -- seeded with the manifest default.
Edits are written straight into ``StudyState.param_values``, which the Run page reads.
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis.state import StudyState

# Fallback bounds when the manifest leaves a numeric parameter unbounded.
_FLOAT_LIMIT = 1.0e12
_INT_LIMIT = 2_147_483_647  # QSpinBox is limited to 32-bit ints


class ParametersPage(QWidget):
    """Renders the model's parameters and captures edited values into study state."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self._empty = QLabel("Select a valid model manifest above to configure its parameters.")
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
                widget.setParent(None)
                widget.deleteLater()

        has_params = manifest is not None and len(manifest.params) > 0
        self._empty.setVisible(not has_params)
        self._form_host.setVisible(has_params)
        if not has_params:
            return

        for name, spec in manifest.params.items():
            control = self._make_control(name, spec)
            self._form.addRow(_label_for(spec), control)
            # Seed study state with the default so a run works without visiting this page.
            self._state.param_values[name] = spec.default

    def _make_control(self, name: str, spec: Any) -> QWidget:
        ptype = str(spec.type)
        if ptype == "float":
            return self._float_control(name, spec)
        if ptype == "int":
            return self._int_control(name, spec)
        if ptype == "bool":
            return self._bool_control(name, spec)
        if ptype == "choice":
            return self._choice_control(name, spec)
        # Unknown type: show it read-only rather than guessing.
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
        if spec.unit:
            box.setSuffix(f" {spec.unit}")
        box.setToolTip(spec.description or "")
        box.setValue(float(spec.default))
        box.valueChanged.connect(lambda value, n=name: self._set(n, float(value)))
        return box

    def _int_control(self, name: str, spec: Any) -> QSpinBox:
        box = QSpinBox()
        box.setRange(
            int(spec.min) if spec.min is not None else -_INT_LIMIT,
            int(spec.max) if spec.max is not None else _INT_LIMIT,
        )
        if spec.unit:
            box.setSuffix(f" {spec.unit}")
        box.setToolTip(spec.description or "")
        box.setValue(int(spec.default))
        box.valueChanged.connect(lambda value, n=name: self._set(n, int(value)))
        return box

    def _bool_control(self, name: str, spec: Any) -> QCheckBox:
        box = QCheckBox()
        box.setToolTip(spec.description or "")
        box.setChecked(bool(spec.default))
        box.toggled.connect(lambda checked, n=name: self._set(n, bool(checked)))
        return box

    def _choice_control(self, name: str, spec: Any) -> QComboBox:
        box = QComboBox()
        choices = list(spec.choices)
        box.addItems([str(c) for c in choices])
        box.setToolTip(spec.description or "")
        if spec.default in choices:
            box.setCurrentIndex(choices.index(spec.default))
        box.currentIndexChanged.connect(lambda index, n=name, c=choices: self._set(n, c[index]))
        return box

    def _set(self, name: str, value: Any) -> None:
        self._state.param_values[name] = value


def _label_for(spec: Any) -> QLabel:
    label = QLabel(f"<b>{spec.name}</b>")
    label.setToolTip(spec.description or "")
    return label
