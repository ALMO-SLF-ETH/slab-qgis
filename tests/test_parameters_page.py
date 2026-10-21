"""Tests for the manifest-driven Parameters page (inputs + parameters, grouped)."""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.parameters_page import ParametersPage, _ResamplePanel
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
    """True if the widget is not inside a raster input's resample sub-panel."""
    parent = widget.parent()
    while parent is not None:
        if isinstance(parent, _ResamplePanel):
            return False
        parent = parent.parent()
    return True


def _param_widgets(page: ParametersPage, cls: type) -> list:
    """Widgets of a type that belong to the parameter/input form (not a resample panel)."""
    return [w for w in page.findChildren(cls) if _outside_panel(w)]


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
    assert len(page.findChildren(QSpinBox)) == 1
    assert len(_choice_combos(page)) == 1
    assert len(page.findChildren(QgsMapLayerComboBox)) == 2  # DEM raster + release vector
    assert _param_widgets(page, QCheckBox) == []


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


def test_editing_spinbox_updates_state_and_clamps(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    spin = next(sb for sb in page.findChildren(QDoubleSpinBox) if sb.maximum() == 60.0)
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
    # declared param groups, then the trailing bucket holding the ungrouped inputs (the always-
    # present release-estimate box is not a manifest group).
    titles = [b.title() for b in page.findChildren(QGroupBox) if b is not page._estimate_box]
    assert titles == ["Physical parameters", "Numerics", "Parameters"]


def test_dependent_param_visibility_follows_controller(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    spin = page.findChildren(QSpinBox)[0]  # rk4_stages, visible_when integrator == "rk4"
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
