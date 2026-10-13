"""End-to-end tests for the Run page: build a run-spec, run the toy model, load outputs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.run_page import RunPage
from almo_sweep_qgis.panels.run_task import RunTask
from almo_sweep_qgis.state import StudyState
from qgis.core import QgsApplication, QgsProject

_TOY = Path(__file__).resolve().parents[2] / "almo-sweep" / "tests" / "fixtures" / "toy_model.py"


@pytest.fixture
def toy_manifest():
    assert engine.AVAILABLE, f"engine unavailable: {engine.IMPORT_ERROR}"
    from almo_sweep.contracts import parse_manifest

    return parse_manifest(
        {
            "model": {"name": "toy", "version": "0.1.0"},
            "invocation": {
                "command": [sys.executable, str(_TOY)],
                "args_template": ["--runspec", "{runspec}"],
            },
            "outputs": {"grid": {"kind": "raster"}},
        }
    )


def test_build_runspec_uses_defaults_and_output_folder(
    qgis_app: QgsApplication, toy_manifest, tmp_path: Path
) -> None:
    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)
    page._name_edit.setText("r1")
    page._out_dir.setFilePath(str(tmp_path))

    spec = page._build_runspec()
    assert spec.run_id == "r1"
    assert spec.output_dir.endswith("r1")
    assert spec.store_outputs == ("grid",)


def test_missing_inputs_enforces_visible_conditional_input(qgis_app: QgsApplication) -> None:
    from almo_sweep.contracts import parse_manifest

    m = parse_manifest(
        {
            "model": {"name": "m", "version": "1"},
            "invocation": {"command": ["x"]},
            "groups": [{"id": "release"}],
            "params": {
                "release_type": {
                    "type": "choice",
                    "default": "POLYGON",
                    "choices": ["POLYGON", "RECTANGLE"],
                    "group": "release",
                }
            },
            "inputs": {
                "zones": {
                    "kind": "vector",
                    "geometry": "polygon",
                    "required": False,
                    "group": "release",
                    "visible_when": [{"param": "release_type", "in": ["POLYGON"]}],
                }
            },
            "outputs": {"g": {"kind": "raster"}},
        }
    )
    state = StudyState()
    state.set_manifest(m)
    page = RunPage(state)
    # POLYGON selected but no layer -> the conditional input is required and missing
    state.param_values["release_type"] = "POLYGON"
    assert page._missing_inputs(m) == ["zones"]
    # RECTANGLE -> the input is not shown, so it does not block the run
    state.param_values["release_type"] = "RECTANGLE"
    assert page._missing_inputs(m) == []


def test_build_runspec_requires_output_folder(qgis_app: QgsApplication, toy_manifest) -> None:
    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)
    page._out_dir.setFilePath("")
    with pytest.raises(ValueError, match="output folder"):
        page._build_runspec()


def test_run_executes_toy_model(qgis_app: QgsApplication, toy_manifest, tmp_path: Path) -> None:
    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)
    page._name_edit.setText("run")
    page._out_dir.setFilePath(str(tmp_path))

    task = RunTask(state.manifest, page._build_runspec())
    assert task.run() is True, task.error_message
    assert task.result is not None and task.result.ok
    assert "grid" in task.result.outputs


def test_load_results_adds_raster_layer(
    qgis_app: QgsApplication, toy_manifest, tmp_path: Path
) -> None:
    pytest.importorskip("osgeo")
    import numpy as np
    from almo_sweep.contracts import RunResult
    from osgeo import gdal, osr

    tif = str(tmp_path / "grid.tif")
    ds = gdal.GetDriverByName("GTiff").Create(tif, 4, 4, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0, 1, 0, 0, 0, -1])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.ones((4, 4), dtype="float32"))
    ds = None

    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)

    before = len(QgsProject.instance().mapLayers())
    loaded = page._load_results(RunResult.success("run", {"grid": tif}))
    assert loaded == 1
    assert len(QgsProject.instance().mapLayers()) == before + 1
