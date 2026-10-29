"""End-to-end tests for the Run page: build a run-spec, run the toy model, load outputs."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from almo_sweep_qgis import engine
from almo_sweep_qgis.panels.run_page import (
    RunPage,
    _copy_dataset,
    _CopyInputsTask,
    _is_within,
    _layer_local_path,
    _same_file,
    _unique_dst,
)
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


def test_build_runspec_uses_project_and_run_folders(
    qgis_app: QgsApplication, toy_manifest, tmp_path: Path
) -> None:
    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)
    page._name_edit.setText("r1")  # the project name
    page._out_dir.setFilePath(str(tmp_path))

    spec = page._build_runspec()
    # <output>/<project>/<project>_000001 ; run id zero-padded to 6 digits
    assert spec.run_id == "r1_000001"
    assert spec.output_dir.endswith(os.path.join("r1", "r1_000001"))
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


def test_layer_local_path_detects_unsaved_layers(qgis_app: QgsApplication, tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    from qgis.core import QgsVectorLayer

    # A memory (scratch) layer has no file on disk.
    mem = QgsVectorLayer("Polygon?crs=EPSG:2056", "scratch", "memory")
    assert mem.isValid()
    assert _layer_local_path(mem) is None

    # A raster loaded from a real file resolves to that absolute path.
    tif = str(tmp_path / "dem.tif")
    _write_raster(tif, [1.0])
    from qgis.core import QgsRasterLayer

    rlayer = QgsRasterLayer(tif, "dem")
    assert rlayer.isValid()
    assert _layer_local_path(rlayer) == tif

    # A bare relative filename (as in the reported failure) is not a usable local path.
    bare = QgsRasterLayer("dem_blatten_2m.tif", "bare")
    assert _layer_local_path(bare) is None


def test_inputs_being_edited_blocks_uncommitted_layer(qgis_app: QgsApplication) -> None:
    from almo_sweep.contracts import parse_manifest
    from qgis.core import QgsVectorLayer

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
    layer = QgsVectorLayer("Polygon?crs=EPSG:2056", "zones", "memory")
    assert layer.isValid()
    QgsProject.instance().addMapLayer(layer)

    state = StudyState()
    state.set_manifest(m)
    state.param_values["release_type"] = "POLYGON"
    state.input_layers["zones"] = layer.id()
    page = RunPage(state)

    # committed, not in edit mode -> allowed
    assert page._inputs_being_edited(m) == []
    # in edit mode -> blocked
    layer.startEditing()
    assert page._inputs_being_edited(m) == ["zones"]
    # leaving edit mode clears it again
    layer.rollBack()
    assert page._inputs_being_edited(m) == []
    # and when the input is not shown (RECTANGLE), an edited layer does not block the run
    layer.startEditing()
    state.param_values["release_type"] = "RECTANGLE"
    assert page._inputs_being_edited(m) == []
    layer.rollBack()


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


def test_stop_button_disabled_until_run_and_noops_when_idle(
    qgis_app: QgsApplication, toy_manifest
) -> None:
    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)
    # No run in flight: Stop is disabled, and clicking it just reports that nothing is running.
    assert page._stop_button.isEnabled() is False
    page._on_stop_clicked()
    assert "No simulation is running" in page._status.toPlainText()


def test_stop_cancels_running_task_and_kills_process(
    qgis_app: QgsApplication, tmp_path: Path
) -> None:
    # A cancelled RunTask must kill the model subprocess and report as terminated (run() -> False)
    # with a "cancelled" result, rather than waiting out the model's long sleep.
    import time

    from almo_sweep.contracts import RunSpec, parse_manifest

    manifest = parse_manifest(
        {
            "model": {"name": "toy", "version": "0.1.0"},
            "invocation": {
                "command": [sys.executable, str(_TOY)],
                "args_template": ["--runspec", "{runspec}"],
            },
            "outputs": {"grid": {"kind": "raster"}},
        }
    )
    spec = RunSpec(
        run_id="run",
        model_name="toy",
        params={"mode": "sleep"},
        inputs={},
        output_dir=str(tmp_path / "run"),
        store_outputs=("grid",),
    )
    task = RunTask(manifest, spec)
    task.cancel()  # pre-cancel: the engine's watcher kills the process immediately

    started = time.monotonic()
    assert task.run() is False  # cancelled -> taskTerminated, not completed
    assert time.monotonic() - started < 5.0  # did not wait out the 30s sleep
    assert task.result is not None and not task.result.ok
    assert "cancelled" in (task.result.error or "")


def test_apply_pending_resamples_runs_armed_request(
    qgis_app: QgsApplication, tmp_path: Path
) -> None:
    pytest.importorskip("osgeo")
    import numpy as np
    from almo_sweep.contracts import parse_manifest
    from almo_sweep_qgis.dem.resample import raster_resolution
    from osgeo import gdal
    from qgis.core import QgsRasterLayer

    manifest = parse_manifest(
        {
            "model": {"name": "m", "version": "1"},
            "invocation": {"command": ["x"]},
            "inputs": {"dem": {"kind": "raster"}},
            "outputs": {"g": {"kind": "raster"}},
        }
    )
    src = str(tmp_path / "dem.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, 8, 8, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0, 4, 0, 0, 0, -4])  # 4 m cells
    ds.GetRasterBand(1).WriteArray(np.ones((8, 8), dtype="float32"))
    ds = None
    layer = QgsRasterLayer(src, "dem")
    QgsProject.instance().addMapLayer(layer)

    state = StudyState()
    state.set_manifest(manifest)
    state.input_layers["dem"] = layer.id()
    state.resample_requests["dem"] = {"resolution": 8.0, "alg": "average", "out_path": ""}
    page = RunPage(state)

    # A resample job is built; drive it synchronously (as the pipeline controller would).
    jobs = page._pending_resample_jobs(manifest)
    assert len(jobs) == 1
    job = jobs[0]
    task = job.make_task()
    assert task.run() is True
    job.finalize()

    new_id = state.input_layers["dem"]
    assert new_id != layer.id()  # re-pointed to the resampled raster
    new_layer = QgsProject.instance().mapLayer(new_id)
    assert new_layer is not None
    assert raster_resolution(new_layer.source().split("|", 1)[0]) == (8.0, 8.0)


def test_apply_pending_resamples_skips_when_already_at_target(
    qgis_app: QgsApplication, tmp_path: Path
) -> None:
    pytest.importorskip("osgeo")
    import numpy as np
    from almo_sweep.contracts import parse_manifest
    from osgeo import gdal
    from qgis.core import QgsRasterLayer

    manifest = parse_manifest(
        {
            "model": {"name": "m", "version": "1"},
            "invocation": {"command": ["x"]},
            "inputs": {"dem": {"kind": "raster"}},
            "outputs": {"g": {"kind": "raster"}},
        }
    )
    src = str(tmp_path / "dem.tif")
    ds = gdal.GetDriverByName("GTiff").Create(src, 8, 8, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([0, 4, 0, 0, 0, -4])  # 4 m cells
    ds.GetRasterBand(1).WriteArray(np.ones((8, 8), dtype="float32"))
    ds = None
    layer = QgsRasterLayer(src, "dem")
    QgsProject.instance().addMapLayer(layer)

    state = StudyState()
    state.set_manifest(manifest)
    state.input_layers["dem"] = layer.id()
    # Target equals the native resolution -> no job built, input left untouched.
    state.resample_requests["dem"] = {"resolution": 4.0, "alg": "bilinear", "out_path": ""}
    page = RunPage(state)

    assert page._pending_resample_jobs(manifest) == []
    assert state.input_layers["dem"] == layer.id()


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


def test_load_results_unpacks_bands_into_one_group(
    qgis_app: QgsApplication, toy_manifest, tmp_path: Path
) -> None:
    pytest.importorskip("osgeo")
    from almo_sweep.contracts import RunResult

    grid = str(tmp_path / "final_grid.tif")
    _write_raster(grid, [1.0, 2.0, 3.0])  # 3-band raster -> 3 single-band layers

    state = StudyState()
    state.set_manifest(toy_manifest)
    page = RunPage(state)

    loaded = page._load_results(RunResult.success("runX", {"grid": grid}))
    assert loaded == 3  # one layer per band
    root = QgsProject.instance().layerTreeRoot()
    run_group = root.findGroup("runX")
    assert run_group is not None  # all outputs grouped under the run
    assert run_group.findGroup("grid") is not None  # bands grouped under the output name
    assert len(run_group.findGroup("grid").findLayers()) == 3


def _sweepable_manifest():
    """A toy manifest with one sweepable float parameter, driven by the toy model binary."""
    from almo_sweep.contracts import parse_manifest

    return parse_manifest(
        {
            "model": {"name": "toy", "version": "0.1.0"},
            "invocation": {
                "command": [sys.executable, str(_TOY)],
                "args_template": ["--runspec", "{runspec}"],
            },
            "params": {
                "scale": {
                    "type": "float",
                    "default": 1.0,
                    "min": 0.0,
                    "max": 3.0,
                    "sweepable": True,
                }
            },
            "outputs": {"grid": {"kind": "raster"}},
        }
    )


def _grid_sweep_state(manifest: Any) -> StudyState:
    state = StudyState()
    state.set_manifest(manifest)
    state.sweep = {
        "enabled": True,
        "scheme": "grid",
        "n_samples": 50,
        "steps": 4,
        "spacing": "linear",
        "seed": None,
        "params": {"scale": {"dist": "uniform", "low": 0.0, "high": 3.0}},
    }
    return state


def test_build_sweep_jobs_numbers_runs_and_writes_study_json(
    qgis_app: QgsApplication, tmp_path: Path
) -> None:
    manifest = _sweepable_manifest()
    page = RunPage(_grid_sweep_state(manifest))
    page._name_edit.setText("myproj")
    page._out_dir.setFilePath(str(tmp_path))
    page._workers_spin.setValue(3)
    page._retries_spin.setValue(2)

    project_root = page._project_root()
    jobs = page._build_sweep_jobs(manifest, page._state.sweep, project_root)
    assert len(jobs) == 1  # one scheduler-driven job runs the whole sweep

    task = jobs[0].make_task()
    runs = task._sweep_runs
    assert len(runs) == 4
    # Each sample is a distinct, 6-digit-numbered run under <output>/myproj/.
    assert runs[0].runspec.run_id == "myproj_000001"
    assert runs[-1].runspec.run_id == "myproj_000004"
    assert runs[0].runspec.output_dir.endswith(os.path.join("myproj", "myproj_000001"))
    assert runs[0].runspec.params["scale"] == 0.0 and runs[-1].runspec.params["scale"] == 3.0
    # the Parallel-runs / Retries settings are carried into the scheduler task
    assert task._max_workers == 3 and task._retries == 2

    study = json.loads((Path(project_root) / "study.json").read_text(encoding="utf-8"))
    assert study["project"] == "myproj"
    assert study["count"] == 4
    assert study["scheme"]["kind"] == "grid"
    assert len(study["samples"]) == 4
    assert abs(sum(s["weight"] for s in study["samples"]) - 1.0) < 1e-9


def test_exec_group_visible_only_for_sweep(qgis_app: QgsApplication) -> None:
    manifest = _sweepable_manifest()
    state = StudyState()
    state.set_manifest(manifest)
    page = RunPage(state)
    assert not page._exec_group.isVisibleTo(page)  # no sweep -> hidden
    state.sweep = {**state.sweep, "enabled": True}
    state.sweepChanged.emit()
    assert page._exec_group.isVisibleTo(page)  # sweep on -> shown


def test_is_within_detects_project_containment(tmp_path: Path) -> None:
    inside = tmp_path / "proj" / "inputs" / "dem.tif"
    assert _is_within(str(inside), str(tmp_path / "proj"))
    assert not _is_within(str(tmp_path / "elsewhere" / "dem.tif"), str(tmp_path / "proj"))


def test_copy_dataset_copies_the_whole_dataset(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "dem.asc").write_text("ncols 1\n", encoding="utf-8")
    (src_dir / "dem.prj").write_text("EPSG:2056", encoding="utf-8")  # sidecar, same stem
    (src_dir / "other.asc").write_text("x", encoding="utf-8")  # different stem -> not copied

    dst_dir = tmp_path / "proj" / "inputs"
    dst = str(dst_dir / "dem.asc")
    copied = _copy_dataset(str(src_dir / "dem.asc"), dst)
    assert copied == dst
    assert (dst_dir / "dem.asc").exists() and (dst_dir / "dem.prj").exists()
    assert not (dst_dir / "other.asc").exists()


def test_copy_dataset_renames_sidecars_to_match(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "dem.asc").write_text("grid", encoding="utf-8")
    (src_dir / "dem.prj").write_text("EPSG:2056", encoding="utf-8")

    dst_dir = tmp_path / "inputs"
    dst = str(dst_dir / "dem_1.asc")  # a rename-on-conflict target
    _copy_dataset(str(src_dir / "dem.asc"), dst)
    assert (dst_dir / "dem_1.asc").exists()
    assert (dst_dir / "dem_1.prj").exists()  # sidecar renamed to match the new stem


def test_same_file_and_unique_dst(tmp_path: Path) -> None:
    a = tmp_path / "a.tif"
    a.write_bytes(b"data")
    b = tmp_path / "b.tif"
    b.write_bytes(b"data")
    c = tmp_path / "c.tif"
    c.write_bytes(b"different")
    assert _same_file(str(a), str(b)) is True
    assert _same_file(str(a), str(c)) is False

    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "dem.tif").write_bytes(b"x")
    assert _unique_dst(str(inputs), str(tmp_path / "dem.tif")) == str(inputs / "dem_1.tif")


def _dem_run_page(tmp_path: Path, src: str) -> tuple[RunPage, StudyState]:
    from qgis.core import QgsRasterLayer

    manifest = _sweepable_manifest_with_dem()
    layer = QgsRasterLayer(src, "dem")
    QgsProject.instance().addMapLayer(layer)
    state = StudyState()
    state.set_manifest(manifest)
    state.input_layers["dem"] = layer.id()
    page = RunPage(state)
    page._name_edit.setText("proj")
    page._out_dir.setFilePath(str(tmp_path))
    return page, state


def test_copy_inputs_resolve_and_task(qgis_app: QgsApplication, tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    src = str(tmp_path / "dem.tif")
    _write_raster(src, [1.0])
    page, state = _dem_run_page(tmp_path, src)
    manifest = state.manifest

    inputs_dir = os.path.join(page._project_root(), "inputs")
    assert page._inputs_copy_plan(manifest, inputs_dir) == [("dem", src)]

    copies, reuse = page._resolve_inputs_copy_plan(manifest, inputs_dir)
    assert reuse == {} and copies == [("dem", src, os.path.join(inputs_dir, "dem.tif"))]
    task = _CopyInputsTask(copies)
    assert task.run() is True
    copied = task.result_paths["dem"]
    assert os.path.exists(copied) and _is_within(copied, page._project_root())

    # The run-spec then reads the copied path, not the original.
    page._inputs_override.update(task.result_paths)
    spec = page._build_runspec(run_id="proj_000001", output_dir=str(tmp_path / "proj" / "r"))
    assert spec.inputs["dem"] == copied

    # An input already inside the project is not scheduled for copying again.
    state.input_layers["dem"] = _add_raster_layer(copied)
    assert page._inputs_copy_plan(manifest, inputs_dir) == []


def test_copy_inputs_reuses_identical_existing(qgis_app: QgsApplication, tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    src = str(tmp_path / "dem.tif")
    _write_raster(src, [1.0])
    page, state = _dem_run_page(tmp_path, src)
    inputs_dir = os.path.join(page._project_root(), "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(inputs_dir, "dem.tif"))  # identical file already there

    copies, reuse = page._resolve_inputs_copy_plan(state.manifest, inputs_dir)
    assert copies == []  # nothing to copy — same name, same checksum
    assert reuse == {"dem": os.path.join(inputs_dir, "dem.tif")}


def test_copy_inputs_conflict_rename(qgis_app: QgsApplication, tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    src = str(tmp_path / "dem.tif")
    _write_raster(src, [1.0])
    page, state = _dem_run_page(tmp_path, src)
    inputs_dir = os.path.join(page._project_root(), "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    (Path(inputs_dir) / "dem.tif").write_bytes(b"different data")  # same name, different data

    page._prompt_input_conflict = lambda *a: "rename"  # user chooses "copy under a new name"
    copies, reuse = page._resolve_inputs_copy_plan(state.manifest, inputs_dir)
    assert reuse == {}
    assert copies == [("dem", src, os.path.join(inputs_dir, "dem_1.tif"))]


def test_copy_inputs_conflict_cancel_aborts(qgis_app: QgsApplication, tmp_path: Path) -> None:
    pytest.importorskip("osgeo")
    src = str(tmp_path / "dem.tif")
    _write_raster(src, [1.0])
    page, state = _dem_run_page(tmp_path, src)
    inputs_dir = os.path.join(page._project_root(), "inputs")
    os.makedirs(inputs_dir, exist_ok=True)
    (Path(inputs_dir) / "dem.tif").write_bytes(b"different data")

    page._prompt_input_conflict = lambda *a: "cancel"  # user chooses to inspect manually
    with pytest.raises(RuntimeError, match="different contents"):
        page._resolve_inputs_copy_plan(state.manifest, inputs_dir)


def _sweepable_manifest_with_dem():
    from almo_sweep.contracts import parse_manifest

    return parse_manifest(
        {
            "model": {"name": "toy", "version": "0.1.0"},
            "invocation": {"command": ["x"]},
            "inputs": {"dem": {"kind": "raster"}},
            "outputs": {"grid": {"kind": "raster"}},
        }
    )


def _add_raster_layer(path: str) -> str:
    from qgis.core import QgsRasterLayer

    layer = QgsRasterLayer(path, os.path.basename(path))
    QgsProject.instance().addMapLayer(layer)
    return layer.id()


def test_gui_tool_versions_reports_the_plugin(qgis_app: QgsApplication) -> None:
    from almo_sweep_qgis.panels.run_task import _gui_tool_versions

    tv = _gui_tool_versions()
    assert "gui" in tv
    gui = tv["gui"]
    assert gui["name"] == "almo_sweep_qgis"
    assert gui["version"] is not None  # read from the plugin's metadata.txt
    assert set(gui) >= {"name", "version", "commit", "dirty", "date"}


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

    run_group = QgsProject.instance().layerTreeRoot().insertGroup(0, "run1")
    n = page._load_frame_series("grid_series", str(frames), str(max_values), run_group)
    assert n == 6  # 3 frames x 2 bands (h + v)
    assert page._loaded_frame_series is True
    # the frames live under run1 > grid_series > <band> subgroups
    assert run_group.findGroup("grid_series") is not None

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
