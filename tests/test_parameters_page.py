"""Tests for the manifest-driven Parameters page (inputs + parameters, grouped)."""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.parameters_page import ParametersPage, _ResamplePanel, _SweepEditor
from almo_sweep_qgis.state import StudyState
from qgis.core import QgsApplication
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QLabel,
    QSpinBox,
    QWidget,
)


@pytest.fixture
def manifest(dampm_manifest_path: Path):
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    return engine.load_manifest(str(dampm_manifest_path))


def _outside_panel(widget: QWidget) -> bool:
    """True if the widget is a plain parameter/input control: not inside a resample sub-panel, a
    parameter's sweep editor, or the sweep-method box."""
    parent = widget.parent()
    while parent is not None:
        if isinstance(parent, (_ResamplePanel, _SweepEditor)):
            return False
        if parent.objectName() == "sweepMethodBox":
            return False
        parent = parent.parent()
    return True


def _param_widgets(page: ParametersPage, cls: type) -> list:
    """The parameter/input *value* controls of a type: not in a resample panel or sweep editor,
    and not the sweep checkboxes/toggle (object name starts with 'sweep')."""
    return [
        w
        for w in page.findChildren(cls)
        if _outside_panel(w) and not w.objectName().startswith("sweep")
    ]


def _choice_combos(page: ParametersPage) -> list[QComboBox]:
    """Plain choice combos only (QgsMapLayerComboBox is a QComboBox subclass)."""
    return [
        c
        for c in page.findChildren(QComboBox)
        if not isinstance(c, QgsMapLayerComboBox) and _outside_panel(c)
    ]


def test_controls_match_manifest(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    # fixture: 3 float params + 1 int + 1 choice param, plus 2 spatial inputs. Widgets inside the
    # DEM's resample sub-panel are excluded (they are not manifest parameters).
    assert len(_param_widgets(page, QDoubleSpinBox)) == 3
    assert len(_param_widgets(page, QSpinBox)) == 1
    assert len(_choice_combos(page)) == 1
    assert len(page.findChildren(QgsMapLayerComboBox)) == 2  # DEM raster + release vector
    assert _param_widgets(page, QCheckBox) == []  # no bool params (sweep checkboxes excluded)


def test_defaults_are_seeded_into_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    ParametersPage(state)
    assert state.param_values["friction_angle"] == 30.0
    assert state.param_values["density"] == 2000.0
    assert state.param_values["integrator"] == "rk4"


def test_inputs_are_captured_into_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    ParametersPage(state)
    # both inputs are present as keys (value is a layer id or None when nothing is loaded)
    assert set(state.input_layers) == {"dem", "release_zones"}


def test_inputs_default_to_no_layer(qgis_app: QgsApplication, manifest) -> None:
    from qgis.core import QgsProject, QgsVectorLayer

    # Even with a matching layer already in the project, inputs start on "no layer" so the user
    # must pick deliberately.
    existing = QgsVectorLayer("Polygon?crs=EPSG:2056", "already_loaded", "memory")
    QgsProject.instance().addMapLayer(existing)
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    assert all(combo.currentLayer() is None for combo in page._input_combos.values())
    assert all(v is None for v in state.input_layers.values())
    QgsProject.instance().removeMapLayer(existing.id())


def test_editing_spinbox_updates_state_and_clamps(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    spin = next(sb for sb in _param_widgets(page, QDoubleSpinBox) if sb.maximum() == 60.0)
    spin.setValue(45.0)
    assert state.param_values["friction_angle"] == 45.0
    spin.setValue(999.0)  # manifest max is 60 -> widget clamps
    assert state.param_values["friction_angle"] == 60.0


def test_editing_choice_updates_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    combo = _choice_combos(page)[0]  # the integrator choice
    combo.setCurrentIndex(0)  # "euler"
    assert state.param_values["integrator"] == "euler"


def test_params_and_inputs_are_laid_out_in_groups(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    titles = [b.title() for b in page.findChildren(QGroupBox) if b.objectName() != "sweepMethodBox"]
    # declared param groups, then the trailing bucket holding the ungrouped inputs (this fixture
    # has no release section, so no release-estimate rows are added)
    assert titles == ["Physical parameters", "Numerics", "Parameters"]


def test_dependent_param_visibility_follows_controller(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    spin = _param_widgets(page, QSpinBox)[0]  # rk4_stages, visible_when integrator == "rk4"
    assert not spin.isHidden()
    combo = _choice_combos(page)[0]
    combo.setCurrentIndex(combo.findText("euler"))
    assert spin.isHidden()
    combo.setCurrentIndex(combo.findText("rk4"))
    assert not spin.isHidden()


def test_label_uses_manifest_label_not_name(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    texts = [lbl.text() for lbl in page.findChildren(QLabel)]
    assert any("Basal friction angle" in t for t in texts)


def test_empty_without_manifest(qgis_app: QgsApplication) -> None:
    page = ParametersPage(StudyState())
    assert page.findChildren(QDoubleSpinBox) == []
    assert page.findChildren(QgsMapLayerComboBox) == []


def test_clears_on_manifest_reset(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    page = ParametersPage(state)
    state.set_manifest(manifest)
    assert len(_param_widgets(page, QDoubleSpinBox)) == 3
    state.set_manifest(None)
    assert page.findChildren(QDoubleSpinBox) == []
    assert page.findChildren(QgsMapLayerComboBox) == []


# --- parameter sweep (toggle + per-parameter selection live here) ---------------------
def test_sweep_toggle_and_selection_write_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    assert state.sweep["enabled"] is False
    assert set(page._numeric_fields) == {"friction_angle", "release_depth", "density"}

    page._sweep_toggle.setChecked(True)
    assert state.sweep["enabled"] is True
    assert state.sweep["params"] == {}  # nothing ticked yet

    field = page._numeric_fields["density"]
    field._check.setChecked(True)
    assert field._editor._dist.currentData() == "normal"  # Normal is the default profile
    assert not field._control.isVisibleTo(field)  # the fixed-value control is hidden while swept
    # Switch to Uniform and set its range.
    field._editor._dist.setCurrentIndex(field._editor._dist.findData("uniform"))
    field._editor._low.setValue(1800.0)
    field._editor._high.setValue(2200.0)
    assert state.sweep["params"] == {"density": {"dist": "uniform", "low": 1800.0, "high": 2200.0}}


def test_only_visible_sweepable_params_enter_the_sweep(qgis_app: QgsApplication) -> None:
    # A sweepable parameter hidden by a visible_when must not be offered to / kept in the sweep.
    from almo_sweep.contracts import parse_manifest

    m = parse_manifest(
        {
            "model": {"name": "m", "version": "1"},
            "invocation": {"command": ["x"]},
            "params": {
                "law": {"type": "choice", "default": "A", "choices": ["A", "B"]},
                "coef": {
                    "type": "float",
                    "default": 1.0,
                    "min": 0.0,
                    "max": 10.0,
                    "sweepable": True,
                    "visible_when": [{"param": "law", "in": ["A"]}],
                },
            },
            "outputs": {"g": {"kind": "raster"}},
        }
    )
    state = StudyState()
    state.set_manifest(m)
    page = ParametersPage(state)
    page._sweep_toggle.setChecked(True)
    page._numeric_fields["coef"]._check.setChecked(True)
    assert "coef" in state.sweep["params"]  # law == A -> coef visible, in the sweep

    page._set_param("law", "B")  # hides coef (visible_when A)
    assert state.sweep["params"] == {}  # dropped from the sweep while hidden
    page._set_param("law", "A")  # shown again -> re-enters (still ticked)
    assert "coef" in state.sweep["params"]


# --- sweep method (scheme + steps/samples/seed, now on this page) ---------------------
def test_method_box_hidden_until_sweep_enabled(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    assert not page._method_box.isVisibleTo(page)  # hidden while the sweep is off
    page._sweep_toggle.setChecked(True)
    assert page._method_box.isVisibleTo(page)


def test_scheme_and_settings_write_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    page._sweep_toggle.setChecked(True)
    page._scheme_combo.setCurrentIndex(page._scheme_combo.findData("grid"))
    page._steps_spin.setValue(6)
    assert state.sweep["scheme"] == "grid" and state.sweep["steps"] == 6


def test_grid_summary_counts_the_product(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    page._sweep_toggle.setChecked(True)
    page._steps_spin.setValue(4)
    page._numeric_fields["friction_angle"]._check.setChecked(True)
    page._numeric_fields["density"]._check.setChecked(True)
    assert "16" in page._summary.text()  # 4 x 4


def test_scheme_switch_toggles_stack_and_grid_steps(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    page._sweep_toggle.setChecked(True)
    field = page._numeric_fields["friction_angle"]

    page._scheme_combo.setCurrentIndex(page._scheme_combo.findData("monte_carlo"))
    page._samples_spin.setValue(30)
    assert state.sweep["scheme"] == "monte_carlo" and state.sweep["n_samples"] == 30
    assert page._scheme_stack.currentIndex() == 1  # sampler controls
    assert not field._editor._steps.isVisibleTo(field._editor)  # per-param steps hidden for MC

    page._scheme_combo.setCurrentIndex(page._scheme_combo.findData("grid"))
    assert page._scheme_stack.currentIndex() == 0  # grid controls
    assert field._editor._steps.isVisibleTo(field._editor)


def test_seed_only_set_when_fixed(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    page._sweep_toggle.setChecked(True)
    page._scheme_combo.setCurrentIndex(page._scheme_combo.findData("monte_carlo"))
    assert state.sweep["seed"] is None  # "random each run" by default
    page._seed_mode.setCurrentIndex(page._seed_mode.findData(True))
    page._seed_spin.setValue(123)
    assert state.sweep["seed"] == 123


def test_swept_field_is_tinted(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    page._sweep_toggle.setChecked(True)
    field = page._numeric_fields["friction_angle"]
    assert field.styleSheet() == ""  # not swept yet -> no highlight
    field._check.setChecked(True)
    assert "background-color" in field.styleSheet()  # swept -> tinted


# --- conditional input visibility against the real dampm manifest ---------------------
def _dampm_model_toml() -> Path:
    # tests/ -> QGIS_Simulation_GUI -> Simulations
    root = Path(__file__).resolve().parents[2]
    return root / "dampm" / "interface" / "model.toml"


@pytest.mark.skipif(not _dampm_model_toml().exists(), reason="dampm model.toml not generated")
def test_release_sources_show_only_for_their_shape(qgis_app: QgsApplication) -> None:
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    manifest = engine.load_manifest(str(_dampm_model_toml()))
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    # Inputs built in group order: terrain (DEM), then release (release_zones, release_raster).
    combos = page.findChildren(QgsMapLayerComboBox)
    assert len(combos) == 3
    # Each input combo sits inside a '… ' container; that container is the row's field, so its
    # hidden state reflects visibility (the combo itself is never explicitly hidden).
    polygon_row = combos[1].parentWidget()
    raster_row = combos[2].parentWidget()

    page._set_param("release_type", "POLYGON")
    assert not polygon_row.isHidden()
    assert raster_row.isHidden()

    page._set_param("release_type", "RASTER")
    assert polygon_row.isHidden()
    assert not raster_row.isHidden()

    page._set_param("release_type", "RECTANGLE")
    assert polygon_row.isHidden()
    assert raster_row.isHidden()


@pytest.mark.skipif(not _dampm_model_toml().exists(), reason="dampm model.toml not generated")
def test_release_estimate_lives_in_release_group_and_updates(qgis_app: QgsApplication) -> None:
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    manifest = engine.load_manifest(str(_dampm_model_toml()))
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)

    assert page._area_label is not None and page._volume_label is not None
    # the estimate sits inside the release group box (not a page-level box)
    box = page._area_label.parentWidget()
    while box is not None and not isinstance(box, QGroupBox):
        box = box.parentWidget()
    assert box is not None and "release" in box.title().lower()

    page._set_param("release_type", "RECTANGLE")
    page._set_param("rect_Lx", 4.0)
    page._set_param("rect_Ly", 3.0)
    page._set_param("h0", 2.0)
    assert "12.0 m" in page._area_label.text()
    assert "24.0 m" in page._volume_label.text()


@pytest.mark.skipif(not _dampm_model_toml().exists(), reason="dampm model.toml not generated")
def test_release_estimate_uses_mean_for_normal_and_range_for_uniform(
    qgis_app: QgsApplication,
) -> None:
    # When release depth (h0) is swept, the estimate follows its distribution: Normal -> mean
    # (a single central figure); Uniform -> a low-high range for the depth-dependent volume.
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    manifest = engine.load_manifest(str(_dampm_model_toml()))
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)

    page._set_param("release_type", "RECTANGLE")
    page._set_param("rect_Lx", 4.0)
    page._set_param("rect_Ly", 3.0)  # fixed footprint -> area is 12 m^2 throughout
    assert page._volume_label is not None and page._area_label is not None

    page._sweep_toggle.setChecked(True)
    h0 = page._numeric_fields["h0"]
    h0._check.setChecked(True)
    assert h0._editor._dist.currentData() == "normal"  # Normal is the default profile
    h0._editor._mean.setValue(2.0)
    # Normal -> mean: a single central estimate, 12 m^2 x 2 m = 24 m^3.
    assert "24.0 m" in page._volume_label.text()
    assert " to " not in page._volume_label.text()

    # Uniform -> a low-to-high range over the bounds: 12 x 1 = 12 m^3 to 12 x 3 = 36 m^3.
    h0._editor._dist.setCurrentIndex(h0._editor._dist.findData("uniform"))
    h0._editor._low.setValue(1.0)
    h0._editor._high.setValue(3.0)
    assert "12.0 m" in page._area_label.text()  # depth does not change the footprint
    assert " to " in page._volume_label.text()
    assert "12.0 m³" in page._volume_label.text()
    assert "36.0 m³" in page._volume_label.text()

    # Turning the sweep off reverts to the fixed depth (default 2.0 m) -> single 24 m^3 again.
    page._sweep_toggle.setChecked(False)
    assert "24.0 m" in page._volume_label.text()
    assert " to " not in page._volume_label.text()


@pytest.mark.skipif(not _dampm_model_toml().exists(), reason="dampm model.toml not generated")
def test_release_estimate_updates_when_polygon_is_edited_and_committed(
    qgis_app: QgsApplication,
) -> None:
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

    manifest = engine.load_manifest(str(_dampm_model_toml()))
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)

    layer = QgsVectorLayer("Polygon?crs=EPSG:2056", "rel", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,10 0,10 10,0 10,0 0))"))  # 100 m²
    layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)

    page._input_combos["release_zones"].setLayer(layer)
    page._set_param("release_type", "POLYGON")
    page._set_param("h0", 2.0)
    assert page._area_label is not None
    assert "100.0 m" in page._area_label.text()

    # Editing the geometry refreshes the estimate immediately -- even before committing.
    layer.startEditing()
    fid = next(layer.getFeatures()).id()
    layer.changeGeometry(fid, QgsGeometry.fromWkt("POLYGON((0 0,20 0,20 10,0 10,0 0))"))  # 200 m²
    assert "200.0 m" in page._area_label.text()  # updated on the live (uncommitted) edit
    layer.commitChanges()
    assert "200.0 m" in page._area_label.text()  # and still correct after the commit


@pytest.mark.skipif(not _dampm_model_toml().exists(), reason="dampm model.toml not generated")
def test_deleting_selected_layer_reverts_to_no_layer(qgis_app: QgsApplication) -> None:
    from qgis.core import QgsProject, QgsVectorLayer

    manifest = engine.load_manifest(str(_dampm_model_toml()))
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)

    layer = QgsVectorLayer("Polygon?crs=EPSG:2056", "rel", "memory")
    QgsProject.instance().addMapLayer(layer)
    combo = page._input_combos["release_zones"]
    combo.setLayer(layer)
    page._set_param("release_type", "POLYGON")
    assert combo.currentLayer() is layer

    # Removing the selected layer must revert to "no layer" without touching the dangling layer.
    QgsProject.instance().removeMapLayer(layer.id())
    assert combo.currentLayer() is None
    assert state.input_layers["release_zones"] is None
    assert page._area_label is not None and page._area_label.text() == "—"
