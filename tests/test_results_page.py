"""Headless tests for the Results page: loading a sweep and populating the table."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from almo_sweep_qgis.panels.results_page import ResultsPage, _num_cell
from almo_sweep_qgis.results.aggregate import AggregateConfig, aggregate_max_values
from almo_sweep_qgis.results.study import load_study
from almo_sweep_qgis.state import StudyState
from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt

GT = (0.0, 10.0, 0.0, 0.0, 0.0, -10.0)


def _write_maxvals(path: Path, h: list[list[float]], v: list[list[float]]) -> None:
    from osgeo import gdal

    gdal.UseExceptions()
    ha, va = np.array(h, dtype=np.float32), np.array(v, dtype=np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), ha.shape[1], ha.shape[0], 2, gdal.GDT_Float32
    )
    ds.SetGeoTransform(GT)
    for band, arr in ((1, ha), (2, va)):
        b = ds.GetRasterBand(band)
        b.SetNoDataValue(-9999.0)
        b.WriteArray(arr)
    ds.FlushCache()
    ds = None


def _make_project(root: Path) -> None:
    for i, h in enumerate((1.0, 5.0), start=1):
        run = root / f"p_{i:06d}"
        run.mkdir(parents=True)
        mv = run / "max_values.tif"
        _write_maxvals(mv, [[0.0, h]], [[0.0, h * 0.5]])
        (run / "result.json").write_text(
            json.dumps({"outputs": {"max_values": str(mv)}}), encoding="utf-8"
        )
    (root / "study.json").write_text(
        json.dumps(
            {
                "project": "p",
                "scheme_key": "monte_carlo",
                "seed": 1,
                "count": 2,
                "swept": {"scale": {"dist": "uniform", "low": 1.0, "high": 5.0}},
            }
        ),
        encoding="utf-8",
    )
    (root / "runs.json").write_text(
        json.dumps(
            {
                "manifest": {"name": "dampm", "version": "1.0"},
                "runs": [
                    {
                        "run_id": "p_000001",
                        "status": "success",
                        "dir": str(root / "p_000001"),
                        "params": {"scale": 1.0},
                        "weight": 0.25,
                        "duration_s": 2.0,
                    },
                    {
                        "run_id": "p_000002",
                        "status": "success",
                        "dir": str(root / "p_000002"),
                        "params": {"scale": 5.0},
                        "weight": 0.75,
                        "duration_s": 4.0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_results_page_loads_and_populates(qgis_app: QgsApplication, tmp_path: Path) -> None:
    _make_project(tmp_path)
    page = ResultsPage(StudyState())
    page._folder.setFilePath(str(tmp_path))
    page._on_load()

    assert page._table.rowCount() == 2
    assert page._compute_btn.isEnabled()  # there are successful runs to aggregate
    assert "monte_carlo" in page._header.text()
    assert "scale" in page._header.text().lower()  # the swept parameter is reported
    # the swept-parameter value appears in the first data row
    texts = {page._table.item(0, c).text() for c in range(page._table.columnCount())}
    assert "p_000001" in texts


def test_table_sorting_is_enabled(qgis_app: QgsApplication, tmp_path: Path) -> None:
    _make_project(tmp_path)
    page = ResultsPage(StudyState())
    page._folder.setFilePath(str(tmp_path))
    page._on_load()
    assert page._table.isSortingEnabled()  # headers are clickable to sort


def test_numeric_cells_sort_by_value_not_text(qgis_app: QgsApplication) -> None:
    # The lexical trap: "10" < "9" as text, but 10 > 9 as numbers. _num_cell must sort numerically.
    nine = _num_cell("9", 9.0)
    ten = _num_cell("10", 10.0)
    assert nine < ten
    assert not (ten < nine)
    # A blank numeric cell (no key) sorts after any value, regardless of direction.
    blank = _num_cell("", None)
    assert ten < blank
    assert nine < blank


def test_sorting_a_swept_column_reorders_rows(qgis_app: QgsApplication, tmp_path: Path) -> None:
    _make_project(tmp_path)
    page = ResultsPage(StudyState())
    page._folder.setFilePath(str(tmp_path))
    page._on_load()
    # Columns: Run, Status, Weight, <scale>, Max depth, ... -> the swept "scale" column is index 3.
    scale_col = 3

    page._table.sortItems(scale_col, Qt.SortOrder.AscendingOrder)
    assert page._table.item(0, 0).text() == "p_000001"  # scale 1.0 first
    page._table.sortItems(scale_col, Qt.SortOrder.DescendingOrder)
    assert page._table.item(0, 0).text() == "p_000002"  # scale 5.0 first


def test_metrics_fill_correct_run_after_sorting(qgis_app: QgsApplication, tmp_path: Path) -> None:
    # After the user sorts, filling per-run metrics must still land on the right run's row.
    from almo_sweep_qgis.results.aggregate import AggregateInput

    _make_project(tmp_path)
    page = ResultsPage(StudyState())
    page._folder.setFilePath(str(tmp_path))
    page._on_load()
    page._table.sortItems(3, Qt.SortOrder.DescendingOrder)  # p_000002 (scale 5.0) now on top

    summary = load_study(tmp_path)
    inputs = [
        AggregateInput(r.run_id, r.max_values_path(), r.weight) for r in summary.successful_runs()
    ]
    result = aggregate_max_values(inputs, tmp_path / "aggregates", AggregateConfig(threshold=0.1))
    page._fill_metrics(result)

    # Max depth for p_000002 is 5.0; its row is 0 after the descending sort.
    base = page._metric_base
    assert page._table.item(0, 0).text() == "p_000002"
    assert page._table.item(0, base).text() == "5"


def test_load_full_run_button_enabled_only_for_a_selected_successful_run(
    qgis_app: QgsApplication, tmp_path: Path
) -> None:
    _make_project(tmp_path)
    page = ResultsPage(StudyState())
    page._folder.setFilePath(str(tmp_path))
    page._on_load()
    assert not page._load_btn.isEnabled()  # nothing selected yet
    page._table.selectRow(0)
    assert page._load_btn.isEnabled()  # a successful run is selected


def test_load_full_run_loads_max_values_and_time_series(
    qgis_app: QgsApplication, tmp_path: Path
) -> None:
    from qgis.core import QgsProject

    _make_project(tmp_path)
    # Give run p_000001 a grid_series directory of frames, and record it in its result.json so the
    # Results page loads the full run (max values + time series) like a single run.
    run_dir = tmp_path / "p_000001"
    frames = run_dir / "grid_series"
    frames.mkdir()
    for i in range(3):
        _write_maxvals(frames / f"t_{i:03d}.tif", [[0.0, 1.0 + i]], [[0.0, 0.5 * i]])
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    result["outputs"]["grid_series"] = str(frames)
    (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")

    page = ResultsPage(StudyState())
    page._folder.setFilePath(str(tmp_path))
    page._on_load()
    page._table.selectRow(0 if page._table.item(0, 0).text() == "p_000001" else 1)
    assert page._selected_run().run_id == "p_000001"

    root = QgsProject.instance().layerTreeRoot()
    assert root.findGroup("p_000001") is None
    page._on_load_run()
    group = root.findGroup("p_000001")
    assert group is not None  # the run's outputs are grouped under its id
    # max_values is unpacked per band, and the frames form temporal subgroups.
    assert group.findGroup("max_values") is not None
    series = group.findGroup("grid_series")
    assert series is not None
    assert series.findGroup("flow height h") is not None  # a temporal band subgroup
    assert "p_000001" in page._status.text()


def test_project_finished_sets_folder_and_loads(qgis_app: QgsApplication, tmp_path: Path) -> None:
    _make_project(tmp_path)
    page = ResultsPage(StudyState())
    page._on_project_finished(str(tmp_path))  # as emitted by the Run page after a sweep
    assert page._folder.filePath() == str(tmp_path)
    assert page._table.rowCount() == 2  # auto-loaded because runs.json exists


def test_styled_layer_clips_below_min(qgis_app: QgsApplication, tmp_path: Path) -> None:
    from qgis.core import QgsRasterLayer

    _write_maxvals(tmp_path / "m.tif", [[0.0, 5.0]], [[0.0, 2.0]])
    layer = QgsRasterLayer(str(tmp_path / "m.tif"), "m")
    ResultsPage(StudyState())._style(layer, "impact_probability")
    shader = layer.renderer().shader().rasterShaderFunction()
    assert shader.clip() is True
    assert shader.minimumValue() == 0.001  # dry/near-zero cells render transparent


def test_results_page_aggregation_task_runs(qgis_app: QgsApplication, tmp_path: Path) -> None:
    # Drive the aggregation directly (no task manager) to check the page's inputs produce maps.
    _make_project(tmp_path)
    summary = load_study(tmp_path)
    from almo_sweep_qgis.results.aggregate import AggregateInput

    inputs = [
        AggregateInput(r.run_id, r.max_values_path(), r.weight) for r in summary.successful_runs()
    ]
    result = aggregate_max_values(inputs, tmp_path / "aggregates", AggregateConfig(threshold=0.1))
    assert "impact_probability" in result.layers
    assert result.per_run["p_000002"].max_height == 5.0
