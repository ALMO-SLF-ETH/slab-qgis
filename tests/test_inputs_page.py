"""Tests for the manifest-driven Inputs page and shared study state."""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.inputs_page import InputsPage
from almo_sweep_qgis.state import StudyState
from qgis.core import QgsApplication
from qgis.gui import QgsMapLayerComboBox


@pytest.fixture
def manifest(dampm_manifest_path: Path):
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    return engine.load_manifest(str(dampm_manifest_path))


def test_state_emits_on_manifest_change(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    seen: list[object] = []
    state.manifestChanged.connect(seen.append)
    state.set_manifest(manifest, "somewhere/model.toml")
    assert seen == [manifest]
    assert state.manifest is manifest
    assert state.manifest_path == "somewhere/model.toml"


def test_inputs_render_one_combo_per_input(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.set_manifest(manifest)
    page = InputsPage(state)

    combos = page.findChildren(QgsMapLayerComboBox)
    assert len(combos) == len(manifest.inputs) == 2


def test_inputs_empty_without_manifest(qgis_app: QgsApplication) -> None:
    page = InputsPage(StudyState())
    assert page.findChildren(QgsMapLayerComboBox) == []


def test_inputs_rebuild_clears_previous(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    page = InputsPage(state)
    state.set_manifest(manifest)
    assert len(page.findChildren(QgsMapLayerComboBox)) == 2
    # Clearing the manifest must tear the pickers down again.
    state.set_manifest(None)
    assert page.findChildren(QgsMapLayerComboBox) == []


def test_setting_manifest_resets_input_layers(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    state.input_layers = {"stale": "xyz"}
    state.set_manifest(manifest)
    assert state.input_layers == {}
