"""Tests for the manifest-driven Parameters page."""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.parameters_page import ParametersPage
from almo_sweep_qgis.state import StudyState
from qgis.core import QgsApplication
from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QSpinBox


@pytest.fixture
def manifest(dampm_manifest_path: Path):
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    return engine.load_manifest(str(dampm_manifest_path))


def test_controls_match_manifest(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    # dampm fixture: 3 float params + 1 int + 1 choice, no bools.
    assert len(page.findChildren(QDoubleSpinBox)) == 3
    assert len(page.findChildren(QSpinBox)) == 1
    assert len(page.findChildren(QComboBox)) == 1
    assert page.findChildren(QCheckBox) == []


def test_params_are_laid_out_in_declared_groups(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    titles = [b.title() for b in page.findChildren(QGroupBox)]
    assert titles == ["Physical parameters", "Numerics"]


def test_dependent_param_visibility_follows_controller(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    # rk4_stages (the only int) is visible_when integrator == "rk4" (the default).
    spin = page.findChildren(QSpinBox)[0]
    assert not spin.isHidden()
    combo = page.findChildren(QComboBox)[0]
    combo.setCurrentIndex(combo.findText("euler"))
    assert spin.isHidden()  # hidden once the controlling choice no longer matches
    combo.setCurrentIndex(combo.findText("rk4"))
    assert not spin.isHidden()  # and shown again when it does
    # hidden params keep their value in study state regardless
    combo.setCurrentIndex(combo.findText("euler"))
    assert state.param_values["rk4_stages"] == 4


def test_label_uses_manifest_label_not_name(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    from qgis.PyQt.QtWidgets import QLabel

    texts = [lbl.text() for lbl in page.findChildren(QLabel)]
    assert any("Basal friction angle" in t for t in texts)
    assert not any(">friction_angle<" in t for t in texts)


def test_defaults_are_seeded_into_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    ParametersPage(state)
    assert state.param_values["friction_angle"] == 30.0
    assert state.param_values["density"] == 2000.0
    assert state.param_values["integrator"] == "rk4"


def test_editing_spinbox_updates_state_and_clamps(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    # friction_angle is the float bounded to [0, 60]; identify it by its maximum.
    spin = next(sb for sb in page.findChildren(QDoubleSpinBox) if sb.maximum() == 60.0)
    spin.setValue(45.0)
    assert state.param_values["friction_angle"] == 45.0
    spin.setValue(999.0)  # manifest max is 60 -> widget clamps
    assert state.param_values["friction_angle"] == 60.0


def test_editing_choice_updates_state(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = ParametersPage(state)
    combo = page.findChildren(QComboBox)[0]
    combo.setCurrentIndex(0)  # "euler"
    assert state.param_values["integrator"] == "euler"


def test_empty_without_manifest(qgis_app: QgsApplication) -> None:
    page = ParametersPage(StudyState())
    assert page.findChildren(QDoubleSpinBox) == []


def test_clears_on_manifest_reset(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    page = ParametersPage(state)
    state.set_manifest(manifest)
    assert len(page.findChildren(QDoubleSpinBox)) == 3
    state.set_manifest(None)
    assert page.findChildren(QDoubleSpinBox) == []
