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


def _write_raster(path: str, bands: list[float]) -> None:
    import numpy as np
    from osgeo import gdal, osr

    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, len(bands), gdal.GDT_Float32)
    ds.SetGeoTransform([0, 1, 0, 0, 0, -1])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    for b, value in enumerate(bands, start=1):
        ds.GetRasterBand(b).WriteArray(np.full((4, 4), value, dtype="float32"))
    ds = None


def test_frame_series_loads_two_turbo_temporal_groups(
    qgis_app: QgsApplication, toy_manifest, tmp_path: Path
) -> None:
    pytest.importorskip("osgeo")
    from qgis.core import QgsSingleBandPseudoColorRenderer

    frames = tmp_path / "rasters"
    frames.mkdir()
    for k in range(3):  # 2-band frames: band1 (h), band2 (v)
        _write_raster(str(frames / f"rasters_frame_0{k}.tif"), [float(k), float(k) * 10.0])
    # per-cell maxima over the run -> drive the colour upper bounds (band1=2, band2=20)
    max_values = tmp_path / "max_values.tif"
    _write_raster(str(max_values), [2.0, 20.0])

    state = StudyState()
    state.set_manifest(toy_manifest)
    state.param_values["output_dt"] = 5.0  # frame spacing in seconds
    page = RunPage(state)

    n = page._load_frame_series("grid_series", str(frames), "run1", str(max_values))
    assert n == 6  # 3 frames x 2 bands (h + v)
    assert page._loaded_frame_series is True

    temporal = [
        lyr for lyr in QgsProject.instance().mapLayers().values() if lyr.name().startswith("t = ")
    ]
    pseudo = [
        lyr for lyr in temporal if isinstance(lyr.renderer(), QgsSingleBandPseudoColorRenderer)
    ]
    assert len(pseudo) >= 6
    assert all(lyr.temporalProperties().isActive() for lyr in pseudo)
    # colour upper bound comes from max_values: band 1 -> 2.0, band 2 -> 20.0
    band1 = [lyr for lyr in pseudo if lyr.renderer().band() == 1]
    band2 = [lyr for lyr in pseudo if lyr.renderer().band() == 2]
    assert band1 and all(abs(lyr.renderer().classificationMax() - 2.0) < 1e-6 for lyr in band1)
    assert band2 and all(abs(lyr.renderer().classificationMax() - 20.0) < 1e-6 for lyr in band2)
