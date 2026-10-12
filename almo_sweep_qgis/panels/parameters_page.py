"""The Parameters page: one control per parameter the manifest declares.

Like the Inputs page, this is entirely manifest-driven. Each parameter becomes a control
matched to its type -- a double spin box (float) or spin box (int) honouring the manifest's
bounds and unit, a combo (choice), or a checkbox (bool) -- seeded with the manifest default.
Edits are written straight into ``StudyState.param_values``, which the Run page reads.

Three pieces of the manifest's presentation vocabulary shape the form:

* **label** -- the control's caption is the param's human label (falling back to its name).
* **groups** -- params are laid out under their group's heading, in the manifest's group and
  param order (``ModelManifest.grouped_params``). A manifest with no groups renders as a
  single unnamed section.
* **visible_when** -- a param whose conditions are not met is hidden, and re-shown the moment
  they are (e.g. a friction coefficient appears only for its own friction law). Visibility is
  cosmetic: a hidden param keeps its value in study state and is still sent to the model.
"""

from __future__ import annotations

from typing import Any

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from almo_sweep_qgis.state import StudyState

# Fallback bounds when the manifest leaves a numeric parameter unbounded.
_FLOAT_LIMIT = 1.0e12
_INT_LIMIT = 2_147_483_647  # QSpinBox is limited to 32-bit ints


class _Row:
    """One parameter's place in the form, so visibility can be toggled later."""

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
    """Renders the model's parameters, grouped, and captures edited values into study state."""

    def __init__(self, state: StudyState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._rows: list[_Row] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self._empty = QLabel("Select a valid model manifest above to configure its parameters.")
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
        # Tear down any previous group boxes and their controls.
        while self._groups_layout.count():
            item = self._groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows.clear()

        has_params = manifest is not None and len(manifest.params) > 0
        self._empty.setVisible(not has_params)
        self._groups_host.setVisible(has_params)
        if not has_params:
            return

        for group, specs in manifest.grouped_params():
            if not specs:
                continue
            title = group.display_label or "Parameters"
            box = QGroupBox(title)
            if group.description:
                box.setToolTip(group.description)
            form = QFormLayout(box)
            for spec in specs:
                label = _label_for(spec)
                field = self._make_control(spec.name, spec)
                index = form.rowCount()
                form.addRow(label, field)
                self._rows.append(_Row(spec, form, index, box, label, field))
                # Seed study state with the default so a run works without visiting this page.
                self._state.param_values[spec.name] = spec.default
            self._groups_layout.addWidget(box)

        self._apply_visibility()

    def _apply_visibility(self) -> None:
        """Show or hide each row (and empty group boxes) per the params' current values."""
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
        # Qt 6's QFormLayout has setRowVisible; fall back to toggling the widgets directly.
        setter = getattr(row.form, "setRowVisible", None)
        if callable(setter):
            setter(row.index, visible)
        else:  # pragma: no cover - exercised only on Qt builds without setRowVisible
            row.label.setVisible(visible)
            row.field.setVisible(visible)

    def _make_control(self, name: str, spec: Any) -> QWidget:
        ptype = str(spec.type)
        if ptype == "float":
            return _with_unit(self._float_control(name, spec), spec.unit)
        if ptype == "int":
            return _with_unit(self._int_control(name, spec), spec.unit)
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
        # A change may reveal or hide dependent parameters (visible_when).
        self._apply_visibility()


def _label_for(spec: Any) -> QLabel:
    label = QLabel(f"<b>{spec.display_label}</b>")
    label.setToolTip(spec.description or "")
    return label


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
