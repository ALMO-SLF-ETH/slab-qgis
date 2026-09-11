"""Tests for the Run page's output-selection group."""

from __future__ import annotations

from pathlib import Path

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.run_page import RunPage
from almo_sweep_qgis.state import StudyState
from qgis.core import QgsApplication


@pytest.fixture
def manifest(dampm_manifest_path: Path):
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    return engine.load_manifest(str(dampm_manifest_path))


def test_outputs_group_reflects_manifest(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    page = RunPage(state)
    state.set_manifest(manifest)

    checks = page._output_checks
    assert set(checks) == {"max_depth", "max_pressure", "summary"}
    # Non-optional outputs are always stored: checked and disabled.
    assert checks["max_depth"].isChecked() and not checks["max_depth"].isEnabled()
    # Optional outputs are checked by default but editable.
    assert checks["max_pressure"].isEnabled()
    assert set(page._selected_outputs()) == {"max_depth", "max_pressure", "summary"}


def test_unchecking_optional_output_drops_it(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    page = RunPage(state)
    state.set_manifest(manifest)
    page._output_checks["max_pressure"].setChecked(False)
    assert set(page._selected_outputs()) == {"max_depth", "summary"}


def test_outputs_group_clears_on_reset(qgis_app: QgsApplication, manifest) -> None:
    state = StudyState()
    page = RunPage(state)
    state.set_manifest(manifest)
    assert page._output_checks
    state.set_manifest(None)
    assert page._output_checks == {}
    assert not page._outputs_group.isVisible()
